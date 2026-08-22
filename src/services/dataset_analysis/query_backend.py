"""DuckDB query backend for the analyze_dataset tool.

責務: dataset の backend パス（S3 / ローカル）を VIEW 化し、parameterized SQL を
実行して ``(columns, rows)`` を返す。access allowlist・httpfs LOAD（S3 時のみ）・
S3 認証情報設定・soft timeout（中断・後片付け）をこの層に閉じる。

**timeout は soft**: Python thread は強制終了できず、DuckDB ``close()`` は worker の
終了前に戻る。よって「worker 終了を待つ」と「無期限に停止しない」は thread では両立
しない。本 backend は以下で有界時間の返却を保証する:

1. 同期 query を専用 worker で実行し、watchdog が timeout 超過で ``interrupt()`` を呼ぶ。
2. request スレッドは timeout + 短い join 猶予まで待って戻る（無期限ブロックしない）。
3. worker が interrupt 後も残る場合、connection を強制 close せず worker 自身の終了に
   委ね、Semaphore スロットは worker が実際に終了するまで解放しない
   （＝残存 worker は同時実行枠を占有し続けるので実同時実行数は超えない）。

厳密な wall-clock hard timeout が必要な場合の process 分離は将来対応（スコープ外）。
"""

import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from typing import Any, List, Optional, Protocol, Tuple

logger = logging.getLogger(__name__)


class DatasetAccessError(ValueError):
    """backend パスが access allowlist を通らない場合に送出する。"""


class DatasetQueryTimeout(Exception):
    """query が soft timeout 内に完了しなかった場合に送出する。"""


@dataclass(frozen=True)
class _ViewSpec:
    """検証済みの読み取り対象。``path`` は VIEW 化に使う正規化済みパス。"""

    path: str
    is_s3: bool


class DatasetQueryBackend(Protocol):
    """analyze_dataset の実行 backend。将来 Athena 等へ差し替える継ぎ目。

    timeout・中断・後片付けは backend の責務。Service 側から connection を直接
    interrupt する前提を置かない。
    """

    def execute(
        self,
        source: str,
        sql: str,
        params: List[Any],
        *,
        timeout: float,
    ) -> Tuple[List[str], List[List[Any]]]:
        """source を VIEW 化し SQL を実行。(columns, rows) を返す。

        timeout 超過時は ``DatasetQueryTimeout`` を送出する。
        """
        raise NotImplementedError


class DuckDBQueryBackend:
    """DuckDB による :class:`DatasetQueryBackend` 実装。"""

    def __init__(
        self,
        bucket_name: str = "",
        region_name: str = "us-east-1",
        *,
        allowed_s3_prefixes: Optional[List[str]] = None,
        allowed_local_roots: Optional[List[str]] = None,
        max_concurrent_queries: int = 4,
        join_grace_seconds: float = 1.0,
    ) -> None:
        self.bucket_name = bucket_name
        self.region_name = region_name
        self.allowed_s3_prefixes = allowed_s3_prefixes or [
            "datasets/",
            "persona-dataset/",
        ]
        roots = allowed_local_roots or [
            tempfile.gettempdir(),
            "/tmp",  # save_temp_csv はここに直接書く
            "datasets",  # local:// フォールバックの実体（dataset_manager）
        ]
        # realpath 正規化（/tmp -> /private/tmp 等の差異を吸収）。
        self._allowed_local_roots = sorted({os.path.realpath(r) for r in roots})
        self._sem = threading.BoundedSemaphore(max_concurrent_queries)
        self._join_grace = join_grace_seconds

    # ------------------------------------------------------------------
    # access allowlist
    # ------------------------------------------------------------------

    def _resolve(self, source: str) -> _ViewSpec:
        """backend パスを検証し、正規化済み ``_ViewSpec`` を返す。"""
        if source.startswith("s3://"):
            prefix = f"s3://{self.bucket_name}/"
            if not self.bucket_name or not source.startswith(prefix):
                raise DatasetAccessError("s3 path is not under the configured bucket")
            key = source[len(prefix) :]
            if ".." in key:
                raise DatasetAccessError("s3 key must not contain '..'")
            if not any(key.startswith(p) for p in self.allowed_s3_prefixes):
                raise DatasetAccessError("s3 key is not under an allowed prefix")
            return _ViewSpec(path=source, is_s3=True)

        local = source[len("local://") :] if source.startswith("local://") else source
        real = os.path.realpath(local)
        if not any(
            real == root or real.startswith(root + os.sep)
            for root in self._allowed_local_roots
        ):
            raise DatasetAccessError("local path is outside allowed roots")
        return _ViewSpec(path=real, is_s3=False)

    # ------------------------------------------------------------------
    # execution
    # ------------------------------------------------------------------

    def execute(
        self,
        source: str,
        sql: str,
        params: List[Any],
        *,
        timeout: float,
    ) -> Tuple[List[str], List[List[Any]]]:
        view_spec = self._resolve(source)

        import time

        start = time.monotonic()
        if not self._sem.acquire(timeout=timeout):
            raise DatasetQueryTimeout("timed out waiting for a query slot")
        # Semaphore は worker の finally が解放する（残存 worker が枠を占有し続けるため）。
        acquired_by_worker = False
        try:
            remaining = timeout - (time.monotonic() - start)
            if remaining <= 0:
                raise DatasetQueryTimeout("query budget exhausted before execution")

            holder: dict[str, Any] = {}
            conn_box: dict[str, Any] = {}
            done = threading.Event()

            def worker() -> None:
                conn = None
                try:
                    import duckdb

                    conn = duckdb.connect(":memory:")
                    conn_box["conn"] = conn
                    self._setup_view(conn, view_spec)
                    if params:
                        rel = conn.execute(
                            sql, params
                        )  # nosemgrep: sqlalchemy-execute-raw-query
                    else:
                        rel = conn.execute(
                            sql
                        )  # nosemgrep: sqlalchemy-execute-raw-query
                    columns = [d[0] for d in rel.description]
                    rows = [list(r) for r in rel.fetchall()]
                    holder["result"] = (columns, rows)
                except Exception as e:  # noqa: BLE001 - request 側へ受け渡す
                    holder["error"] = e
                finally:
                    if conn is not None:
                        try:
                            conn.close()
                        except Exception:  # noqa: BLE001 - best effort
                            pass
                    self._sem.release()
                    done.set()

            thread = threading.Thread(target=worker, daemon=True)
            thread.start()
            # start() 成功後は worker の finally が唯一の解放者。start() が
            # 例外で失敗した場合は False のままなので外側 finally が解放する
            # （True を start() 前に立てるとスロットが永久リークする）。
            acquired_by_worker = True

            if not done.wait(timeout=remaining):
                # watchdog: timeout。実行中の query を中断する。
                conn = conn_box.get("conn")
                if conn is not None:
                    try:
                        conn.interrupt()
                    except Exception:  # noqa: BLE001 - best effort
                        pass
                # interrupt が効いて worker が有界時間内に終わっても、request からは
                # timeout として扱う（境界での成功結果は破棄）。worker の finally が
                # connection close と Semaphore 解放を行う（残存中は枠を占有し続ける）。
                done.wait(timeout=self._join_grace)
                raise DatasetQueryTimeout("query exceeded timeout")

            if "error" in holder:
                raise holder["error"]
            columns, rows = holder["result"]
            return columns, rows
        finally:
            # 正常経路（worker 起動前の早期 return / 例外）では request が解放する。
            # worker を起動した後は worker の finally が唯一の解放者。
            if not acquired_by_worker:
                self._sem.release()

    def _setup_view(self, conn: Any, view_spec: _ViewSpec) -> None:
        conn.execute("SET autoinstall_known_extensions=false;")
        conn.execute("SET autoload_known_extensions=false;")
        if view_spec.is_s3:
            # httpfs はコンテナ／ローカルとも事前 INSTALL 済みの前提（runtime は
            # 実行時ダウンロードしない＝閉域では欠落時に LOAD で即失敗させる）。
            # ローカルの事前 INSTALL 手順は docs/local_development.md を参照。
            conn.execute("LOAD httpfs;")  # nosemgrep: sqlalchemy-execute-raw-query
            self._set_s3_credentials(conn)
        escaped = view_spec.path.replace("'", "''")
        if view_spec.path.endswith(".parquet"):
            source = f"read_parquet('{escaped}')"
        else:
            # header=true で 1 行目を常にヘッダー扱いにする（全数値の先頭行を
            # DuckDB がデータと誤判定し column0.. を割り当てて、スキーマ解析
            # （csv.reader）由来の allowlist 列名と乖離するのを防ぐ）。
            source = f"read_csv_auto('{escaped}', header=true)"
        conn.execute(  # nosemgrep: sqlalchemy-execute-raw-query
            f"CREATE VIEW dataset AS SELECT * FROM {source};"
        )

    def _set_s3_credentials(self, conn: Any) -> None:
        """boto3 の認証情報チェーンから S3 認証情報を DuckDB 接続に設定する。"""
        import boto3

        session = boto3.Session()
        credentials = session.get_credentials()
        if credentials:
            creds = credentials.get_frozen_credentials()
            conn.execute(  # nosemgrep: sqlalchemy-execute-raw-query
                "SET s3_access_key_id = $1;", [creds.access_key]
            )  # gitleaks:allow
            conn.execute(  # nosemgrep: sqlalchemy-execute-raw-query
                "SET s3_secret_access_key = $1;", [creds.secret_key]
            )  # gitleaks:allow
            if creds.token:
                conn.execute(  # nosemgrep: sqlalchemy-execute-raw-query
                    "SET s3_session_token = $1;", [creds.token]
                )  # gitleaks:allow
        conn.execute(  # nosemgrep: sqlalchemy-execute-raw-query
            "SET s3_region = $1;", [self.region_name]
        )
