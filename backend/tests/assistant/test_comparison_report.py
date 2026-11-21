from argparse import Namespace
from pathlib import Path
from runpy import run_path

import pytest

from dataagent.engines.agent.specialist.comparison import compare_evidence


def test_generated_report_and_independent_check(tmp_path):
    source = tmp_path / "data.csv"
    source.write_text(
        "group,early,late\nA&X,0,2\nB,10.25,9.20\nall,10.25,11.20\n", encoding="utf8"
    )
    html = tmp_path / "report.html"
    renderer = (
        Path(__file__).resolve().parents[2]
        / "resources/skills/visualization/compare_amounts.py"
    )
    args = Namespace(
        csv=source,
        output=html,
        group="group",
        before="early",
        after="late",
        before_label="前期",
        after_label="本期",
        metric="收入",
        unit="元",
        source_note="订单表",
        title="收入变化",
        total_label="all",
        table_only=True,
    )
    run_path(str(renderer))["render"](args)

    def verify(document):
        return compare_evidence(source.read_bytes(), document)

    original = html.read_bytes()
    checked = verify(original)
    assert checked["total_change"] == "0.95"
    assert ["A&X", "0.00", "2.00", "+2.00", "—"] in checked["rows"]
    assert ["B", "10.25", "9.20", "-1.05", "-10.24%"] in checked["rows"]
    with pytest.raises(ValueError, match="复算"):
        verify(original.replace(b"<td>-1.05</td>", b"<td>-2.05</td>"))
    with pytest.raises(ValueError):
        verify(original.replace(b"-10.24%", b"10.24%"))
    with pytest.raises(ValueError):
        verify(
            original.replace(b"early", b"__old__")
            .replace(b"late&quot;", b"early&quot;")
            .replace(b"__old__", b"late")
        )
    with pytest.raises(ValueError):
        verify(original.replace(b"</html>", b'<img src="external.png"></html>'))
    args.amounts_only = True
    run_path(str(renderer))["render"](args)
    amounts_only = html.read_bytes()
    assert verify(amounts_only)["rows"][-1] == ["合计", "10.25", "11.20", "+0.95"]
    with pytest.raises(ValueError):
        verify(amounts_only.replace(b"+0.95", b"+0.94"))
    source.write_text("group,early,late\nB,10,12\nall,10,13\n", encoding="utf8")
    with pytest.raises(ValueError, match="合计"):
        verify(original)
