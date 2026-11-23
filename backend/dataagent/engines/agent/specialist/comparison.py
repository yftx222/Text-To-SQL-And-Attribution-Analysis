"""生成两期金额简报，或从原始 CSV 独立复算已生成的证据表。"""

import base64
import csv
import io
import json
import shlex
from argparse import Namespace
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import PurePosixPath
from uuid import UUID, uuid4

from langchain.tools import ToolRuntime, tool

from dataagent.engines.agent.common.tools.errors import tool_error
from dataagent.engines.sandbox import resolve_artifact_path
from resources.skills.visualization.compare_amounts import compose


class _ReportText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.paragraphs = []
        self.tables = 0
        self.cell = None
        self.paragraph = None
        self.images = 0
        self.in_style = False
        self.columns = None

    def handle_starttag(self, tag, attrs):
        # 本工具只验收固定模板；自定义布局走 Reviewer 原有复核流程。
        allowed = {
            "html",
            "meta",
            "title",
            "style",
            "h1",
            "h2",
            "p",
            "section",
            "table",
            "thead",
            "tr",
            "th",
            "tbody",
            "td",
            "img",
        }
        if tag not in allowed or any(
            key not in {"lang", "charset", "name", "content", "alt", "src"}
            for key, _ in attrs
        ):
            raise ValueError("文件包含非标准标签或属性，请按自定义报告复核")
        if tag == "style":
            self.in_style = True
        if tag == "img":
            uri = dict(attrs).get("src") or ""
            prefix = "data:image/png;base64,"
            if not uri.startswith(prefix) or not base64.b64decode(
                uri[len(prefix) :], validate=True
            ).startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError("图表不是内嵌 PNG")
            self.images += 1
        if tag == "meta" and dict(attrs).get("name") == "dataagent-comparison":
            if self.columns is not None:
                raise ValueError("报告重复声明了来源列")
            self.columns = json.loads(dict(attrs)["content"])
        if tag == "table":
            self.tables += 1
        elif tag == "tr":
            self.rows.append([])
        elif tag in {"th", "td"}:
            self.cell = []
        elif tag == "p":
            self.paragraph = []

    def handle_endtag(self, tag):
        if tag == "style":
            self.in_style = False
        if tag in {"th", "td"} and self.cell is not None:
            if not self.rows:
                raise ValueError("证据表缺少行")
            self.rows[-1].append("".join(self.cell).strip())
            self.cell = None
        elif tag == "p" and self.paragraph is not None:
            self.paragraphs.append("".join(self.paragraph).strip())
            self.paragraph = None

    def handle_data(self, data):
        if self.in_style and any(
            token in data.lower() for token in ("url(", "@import", "\\")
        ):
            raise ValueError("样式不是标准内联样式，请单独检查资源依赖")
        if self.cell is not None:
            self.cell.append(data)
        if self.paragraph is not None:
            self.paragraph.append(data)


def compare_evidence(csv_bytes, html_bytes):
    """按列名读取冻结的 CSV，再与 HTML 中实际展示的每一格比对。"""
    page = _ReportText()
    page.feed(html_bytes.decode("utf-8"))
    page.close()
    if not isinstance(page.columns, dict):
        raise TypeError("旧版或自定义报告没有标准列映射，请按原始 CSV 单独复核")
    group, before, after = (page.columns[k] for k in ("group", "before", "after"))
    total_label = page.columns.get("total_label")
    expected, totals = _source_amounts(csv_bytes, group, before, after, total_label)
    return _compare_rows(page, expected, totals)


def _source_amounts(csv_bytes, group, before, after, total_label):
    reader = csv.DictReader(io.StringIO(csv_bytes.decode("utf-8-sig"), newline=""))
    if (
        not {group, before, after}.issubset(reader.fieldnames or [])
        or len({group, before, after}) != 3
    ):
        raise ValueError(f"请核对分组和两期金额列。源表表头 {reader.fieldnames}")
    expected = {}
    for row in reader:
        key = row[group]
        if not key or key in expected:
            raise ValueError("源表分组为空或重复")
        expected[key] = (Decimal(row[before]), Decimal(row[after]))
    stated = expected.pop(total_label) if total_label is not None else None
    if not expected or any(
        not n.is_finite() for pair in expected.values() for n in pair
    ):
        raise ValueError("源表缺少分组或金额无效")
    totals = tuple(sum(pair[i] for pair in expected.values()) for i in (0, 1))
    if stated is not None and stated != totals:
        raise ValueError("源表合计与分组合计不符")
    return expected, totals


def _compare_rows(page, expected, totals):
    if (
        page.tables != 1
        or len(page.rows) != len(expected) + 2
        or page.rows[-1][0:1] != ["合计"]
    ):
        raise ValueError("HTML 的证据表数量、分组行数或合计行不符")
    show_rate = page.columns.get("show_rate", True)
    if not isinstance(show_rate, bool):
        raise TypeError("变化率列标记无效")
    suffix = ["变化", "变化率"] if show_rate else ["变化"]
    width = 3 + len(suffix)
    if len(page.rows[0]) != width or page.rows[0][3:] != suffix:
        raise ValueError("此文件不符合标准两期简报的列布局，请按实际报告复核")
    seen = set()
    verified = []
    for idx, cells in enumerate(page.rows[1:]):
        is_total = idx == len(expected)
        if len(cells) != width or (
            not is_total and (cells[0] not in expected or cells[0] in seen)
        ):
            raise ValueError(f"证据表分组或列数有误：{cells}")
        seen.add(cells[0])
        a, b = totals if is_total else expected[cells[0]]
        wanted = [f"{a:,.2f}", f"{b:,.2f}", f"{b - a:+,.2f}"]
        if show_rate:
            wanted.append(f"{(b - a) / a * 100:+,.2f}%" if a else "—")
        if cells[1:] != wanted:
            raise ValueError(f"{cells[0]} 展示为 {cells[1:]}，从源表复算应为 {wanted}")
        verified.append(cells)
    return {
        "groups": len(expected),
        "headers": page.rows[0],
        "rows": verified,
        "report_text": page.paragraphs,
        "total_change": str(totals[1] - totals[0]),
        "embedded_pngs": page.images,
        "source_columns": page.columns,
    }


def _input(runtime, index, role, suffix):
    state = runtime.state
    inputs = state["delegation_records"][state["current_delegation_id"]]["task"][
        "inputs"
    ]
    if index < 0 or index >= len(inputs):
        raise ValueError("输入编号超出本轮委派 inputs 范围（从 0 开始）")
    ref = inputs[index]
    path = ref["path"]
    cid = UUID(str(runtime.config["configurable"]["conversation_id"]))
    artifact = resolve_artifact_path(path, cid)
    if (
        ref["agent_type"] != role
        or not artifact.relative_path.startswith("evidence/")
        or not path.lower().endswith(suffix)
    ):
        raise ValueError(f"inputs[{index}] 必须是本轮声明的 {role} 固定 {suffix} 文件")
    return path


def comparison_tool(role, backend):
    @tool
    def render_amount_comparison(
        runtime: ToolRuntime,
        group: str,
        before: str,
        after: str,
        before_label: str,
        after_label: str,
        metric: str,
        unit: str,
        source_note: str,
        title: str,
        csv_index: int = 0,
        total_label: str | None = None,
        include_chart: bool = False,
        include_rate: bool = False,
        chart_only: bool = False,
    ) -> dict:
        """从两期原始汇总 CSV 生成固定 HTML；只要各组金额变化图时设 chart_only=True，直接返回固定 PNG。group/before/after 对应实际表头。"""
        try:
            source = _input(runtime, csv_index, "explorer", ".csv")
            path = f"{backend.workspace_dir}/{uuid4()}.html"
            args = Namespace(
                csv=PurePosixPath(source),
                group=group,
                before=before,
                after=after,
                before_label=before_label,
                after_label=after_label,
                metric=metric,
                unit=unit,
                source_note=source_note,
                title=title,
                total_label=total_label,
                amounts_only=not include_rate,
            )
            chart = ""
            if include_chart or chart_only:
                # Matplotlib 和中文字体只安装在分析沙箱里。
                cmd = [
                    "python",
                    "/skills/analyst/visualization/compare_amounts.py",
                    f"--output={path}",
                ]
                if chart_only:
                    cmd.append("--chart-only")
                for key in (
                    "csv",
                    "group",
                    "before",
                    "after",
                    "before_label",
                    "after_label",
                    "metric",
                    "unit",
                    "source_note",
                    "title",
                    "total_label",
                ):
                    value = getattr(args, key)
                    if value is not None:
                        cmd.append(f"--{key.replace('_', '-')}={value}")
                chart_result = backend.execute(shlex.join(cmd))
                if chart_result.exit_code != 0:
                    return tool_error("图表生成失败", chart_result.output[-1000:])
                if chart_only:
                    return {
                        "status": "success",
                        "source": source,
                        "path": backend.freeze_file(
                            path.removesuffix(".html") + ".png"
                        ),
                    }
                (png,) = backend.download_files([path.removesuffix(".html") + ".png"])
                if png.error:
                    return tool_error("图表读取失败", png.error)
                chart = (
                    '<img alt="各分组本期比上期的金额变化" src="data:image/png;base64,'
                    + base64.b64encode(png.content).decode("ascii")
                    + '">'
                )
            (src,) = backend.download_files([source])
            if src.error:
                return tool_error("简报来源读取失败", src.error)
            html, summary = compose(
                args,
                io.StringIO(src.content.decode("utf-8-sig"), newline=""),
                (lambda changes: chart) if include_chart else None,
            )
            (saved,) = backend.upload_files([(path, html.encode("utf-8"))])
            if saved.error:
                return tool_error("简报写入失败", saved.error)
            response = {
                "status": "success",
                "path": backend.freeze_file(path),
                "source": source,
                "summary": summary,
            }
            if include_chart:
                response["chart_preview"] = path.removesuffix(".html") + ".png"
            return response
        except (ValueError, KeyError, InvalidOperation, OSError) as exc:
            return tool_error("简报输入不可用", exc)

    @tool
    def verify_amount_comparison(
        runtime: ToolRuntime,
        csv_index: int = 1,
        group: str = "",
        before: str = "",
        after: str = "",
        total_label: str | None = None,
    ) -> dict:
        """核验标准 HTML；对 PNG 传入源 CSV 的分组、前期、本期列与已有合计行标签，独立复算后须目视核对实际图片。"""
        try:
            source = _input(runtime, csv_index, "explorer", ".csv")
            report = _input(runtime, 0, "analyst", (".html", ".png"))
            src, doc = backend.download_files([source, report])
            if src.error or doc.error:
                return tool_error("无法读取本轮固定输入", src.error or doc.error)
            png = report.lower().endswith(".png")
            if png:
                if not doc.content.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise ValueError("被审文件缺少 PNG 签名")
                amounts, totals = _source_amounts(
                    src.content, group, before, after, total_label
                )
                checks = {
                    "source_columns": [group, before, after],
                    "changes": {k: str(b - a) for k, (a, b) in amounts.items()},
                    "totals": [str(x) for x in totals],
                    "total_change": str(totals[1] - totals[0]),
                }
            else:
                checks = compare_evidence(src.content, doc.content)
            path = f"{backend.workspace_dir}/{uuid4()}.json"
            proof = {
                "source": source,
                "report": report,
                **checks,
            }
            (saved,) = backend.upload_files(
                [(path, json.dumps(proof, ensure_ascii=False).encode())]
            )
            if saved.error:
                return tool_error("复核证据写入失败", saved.error)
            if png:
                return {
                    "status": "success",
                    "path": backend.freeze_file(path),
                    "target": report,
                    "visual_check_required": True,
                    **checks,
                }
            return {
                "status": "success",
                "path": backend.freeze_file(path),
                "target": report,
                "groups_checked": checks["groups"],
                "total_change": checks["total_change"],
                "total_row": checks["rows"][-1],
                "headers": checks["headers"],
                "source_columns": checks["source_columns"],
                "report_text": checks["report_text"],
                "embedded_pngs": checks["embedded_pngs"],
            }
        except (ValueError, TypeError, KeyError, InvalidOperation, OSError) as exc:
            return tool_error("简报核对未通过", exc)

    return render_amount_comparison if role == "analyst" else verify_amount_comparison
