from pathlib import Path


_REPO = Path(__file__).resolve().parents[1]
_EMAIL_LIBRARY = _REPO / "static" / "js" / "emailLibrary.js"


def _bulk_action_source() -> str:
    text = _EMAIL_LIBRARY.read_text(encoding="utf-8")
    start = text.index("async function _bulkAction(action)")
    end = text.index("\n}\n\n// _extractName", start) + 3
    return text[start:end]


def test_bulk_flag_actions_use_single_bulk_flag_request():
    """Bulk done/read/unread must issue ONE /bulk-flag request over all UIDs,
    not a per-UID loop of mark-read/mark-answered."""
    src = _bulk_action_source()
    assert "Local toggle for now" not in src
    assert "/api/email/bulk-flag" in src
    assert "JSON.stringify" in src
    # done sets both flags; read/unread toggle \Seen via add/remove
    assert "Answered" in src and "Seen" in src


def test_bulk_flag_checks_backend_success_before_syncing_cache():
    src = _bulk_action_source()
    assert "data?.success === false" in src
    assert "throw new Error(data?.error" in src
    assert "_libCacheWriteBack()" in src
