"""Assertions on dialogs rendered by ``components/dialog.html`` (U3, contract C6)."""

import re


def dialog_markup(html: str, dialog_id: str) -> str:
    """Return the opening tags of a shared dialog after checking its accessibility.

    The dialog must be the ``dialog()`` macro output: an outer ``[data-dialog]``
    that starts ``hidden``, a ``role="dialog"`` panel with ``aria-modal`` and
    ``aria-labelledby`` pointing at its ``h2`` title.
    """
    outer = re.search(rf'<div id="{re.escape(dialog_id)}" data-dialog[^>]*>', html)
    assert outer, f"{dialog_id} が dialog() で描かれていない"
    assert re.search(r"\shidden(\s|>|=)", outer.group(0)), (
        f"{dialog_id} が始めから見えている"
    )
    rest = html[outer.end() :]
    panel = re.search(r'<div role="dialog"[^>]*>', rest)
    assert panel, f"{dialog_id} に role=dialog がない"
    assert 'aria-modal="true"' in panel.group(0)
    assert f'aria-labelledby="{dialog_id}-title"' in panel.group(0)
    assert re.search(rf'<h2 id="{re.escape(dialog_id)}-title"', rest), "見出しがない"
    return outer.group(0) + panel.group(0)


def dialog_ids(html: str) -> list[str]:
    """Ids of every shared dialog in ``html`` (in document order)."""
    return re.findall(r'<div id="([^"]+)" data-dialog[\s>]', html)
