"""Pure helpers of the organization-asset usage scope (U7 BR5.2, M10).

``DatasetManager``, ``SettingsManager`` and ``SurveyDatasetManager`` each own
one kind of asset and decide with ``AccessPolicy`` themselves. What they share
here holds no decision: naming the departments and laying out the dialog.
(Turning the dialog's values into a change is
:meth:`AssetScopeChange.from_form`, so the routers can build it.)
"""

from typing import Iterable, Sequence

from ...models.authz import Department, ResourceType
from ...models.sharing import AssetScopeDialog

#: Label of a department that no longer exists (M10).
VANISHED_GROUP_LABEL = "（削除されたグループ）"


def scope_labels(
    group_ids: Iterable[str], departments: Sequence[Department]
) -> list[str]:
    """Department names of a scope (deleted ones marked).

    Existing departments come in the directory's order, deleted ones last.
    """
    scope = list(dict.fromkeys(group_ids))
    names = {d.group_id for d in departments}
    known = [d.display_name for d in departments if d.group_id in scope]
    return known + [VANISHED_GROUP_LABEL for g in scope if g not in names]


def scope_dialog(
    resource_type: ResourceType,
    resource_id: str,
    name: str,
    current: frozenset[str],
    departments: Sequence[Department],
) -> AssetScopeDialog:
    """The dialog of one asset: every department, the scope checked (BR5.2)."""
    existing = {d.group_id for d in departments}
    return AssetScopeDialog(
        resource_type=resource_type,
        resource_id=resource_id,
        name=name,
        choices=tuple(departments),
        selected=frozenset(current),
        vanished=tuple(sorted(current - existing)),
    )
