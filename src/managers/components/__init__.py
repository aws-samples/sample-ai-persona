"""Manager Component 層。

複数の Manager が共通で使う **ビジネスワークフロー** を保持する。shared/（純粋
ヘルパー）とは異なり判断ロジックを持ってよいが、Manager そのものではないため
Router/Manager を import してはならない（依存方向は Manager → Component → Service）。
"""
