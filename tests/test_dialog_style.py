from pathlib import Path

from openswap import menubar


def _panel_source() -> str:
    return Path(menubar.__file__).with_name("menubar_panel.py").read_text(
        encoding="utf-8"
    )


def test_all_modal_alerts_use_the_branded_builder():
    source = Path(menubar.__file__).read_text(encoding="utf-8")
    alert = source[source.index("def _alert") : source.index("def _prompt")]
    prompt = source[source.index("def _prompt") : source.index("def _show_error")]
    assert "make_dialog_alert" in alert
    assert "rumps.alert" not in alert
    assert "style_dialog_alert" in prompt


def test_dialogs_share_one_content_width_and_logo():
    source = _panel_source()
    assert "DIALOG_CONTENT_WIDTH = 360.0" in source
    assert "DIALOG_INPUT_HEIGHT = 24.0" in source
    style = source[source.index("def style_dialog_alert") : source.index("def make_dialog_alert")]
    assert "opensoft-symbol-64.png" in source
    assert "_DIALOG_BRAND_IMAGE.setSize_((64, 64))" in source
    assert "alert.setIcon_(icon)" in style
    assert "accessory.setFrameSize_((DIALOG_CONTENT_WIDTH, height))" in style


def test_prompt_callers_do_not_override_the_standard_dimensions():
    source = Path(menubar.__file__).read_text(encoding="utf-8")
    callers = source[source.index("def _make_rename") : source.index("def on_open_log")]
    assert "dimensions=" not in callers
