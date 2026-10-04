# -*- coding: utf-8 -*-
"""html_fixer.py：HTML Fixer Agent —— 依据 Router 分发的审查反馈改 HTML 源码。

完整版方案中 HTML 是**真源**。与早期"整页重写"不同，本版本采用
**结构化补丁（patch ops）+ 确定性应用**：

  * LLM 输出 JSON 补丁指令列表（set_style / replace_text / remove /
    append / insert_after / set_attr / rewrite），**不重写整页 HTML**；
  * 确定性代码（lxml + cssselect）按指令精准修改，输出 token 从整页
    8KB 降到 <1KB，且修改可审计、可回滚；
  * 只有 LLM 判定需要结构级重排时才输出 rewrite（整页重写）作为 fallback。

为什么不用整页重写：
  - 改一个颜色也要重写整页，token 浪费 10 倍以上（且 deepseek-v4-flash
    的 reasoning 会先吃满配额，放大成本）；
  - 整页重写时 LLM 常顺手改动无关区域，是"越改越差、不收敛"的根因；
  - 局部 patch 天然收敛：一次只改反馈指出的位置。

流程（一次调用处理一个 slide 的所有 issue）：
  1. 输入：当前 HTML 源码 + 该页 issue 列表 + 缺数据类 issue 的 RAG 证据；
  2. LLM 输出 JSON patch ops（chat_json，走缓存）；
  3. 确定性应用 -> 沙箱净化 -> 写回；
  4. 返回 diff 摘要（供审计/回滚）。

安全约束（sandbox.py 兜底）：
  - 应用后的 HTML 先过 sanitize_html：删脚本/外链/事件，再落盘；
  - 渲染走 render_html_sandboxed：路由拦截非本地资源。
"""
from __future__ import annotations

import re
from pathlib import Path

from .llm import LLMClient
from .rag_search import format_evidence
from .sandbox import sanitize_html

#: 模板根目录（骨架注入用）：workspace/templates/pages/
_TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "workspace" / "templates" / "pages"


def detect_template(html: str) -> str | None:
    """从渲染后的 HTML 特征类名确定性判断当前页用的模板 ID。

    顺序敏感：先判专用特征（table-wrap/fig-row/flow），再判通用特征
    （grid=content），最后判固定结构页（chapter/toc/cover/closing）。
    识别不到返回 None（自由 HTML 或 Fixer 重写过的页面）。
    """
    if not html:
        return None
    checks = [
        ("big_table", 'class="table-wrap"'),
        ("dual_figure", 'class="fig-row"'),
        ("flow_1x4", 'class="flow"'),
        ("big_figure", "fig-point"),
        ("content", 'class="grid'),
        ("toc", 'class="mod"'),
        ("chapter", 'class="chapter"'),
        ("cover", 'class="cover"'),
        ("closing", "closing"),
    ]
    for tid, marker in checks:
        if marker in html:
            return tid
    return None


def template_skeleton(template_id: str | None) -> str:
    """读取模板骨架：去掉 <style> 后保留 body 结构（占位符即内容区）。

    骨架让执行 LLM 精确知道「模板长什么样、内容该填哪里、哪里是固定结构」，
    而不是只凭截图/坐标猜。找不到模板返回空串（不注入）。
    """
    if not template_id:
        return ""
    path = _TEMPLATE_DIR / f"{template_id}.html"
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8")
    # 去掉 <style>...</style>（骨架只看结构，不看完整样式）
    text = re.sub(r"<style>.*?</style>", "", text, flags=re.DOTALL)
    i = text.find("<body>")
    if i >= 0:
        text = text[i:]
    j = text.find("</body>")
    if j >= 0:
        text = text[: j + len("</body>")]
    return text.strip()


_SYSTEM = """你是论文转 PPT 流水线的 HTML Fixer Agent。你的职责是**精准修改**一页
幻灯片的 HTML 源码来修复审查反馈指出的问题，而不是重写整页。

输入你会拿到：
  1. 该页当前完整 HTML 源码（1280x720 画布，模板引擎生成的组件化结构）；
  2. 本页所用模板的骨架（如有：模板 ID + 去掉样式的 body 结构，占位符就是
     内容区；**这是你判断"固定结构 vs 内容区"的依据**）；
  3. 审查反馈（带规则编号 H1..H6 / S2..S5 或 VLM 描述）；
  4. 缺数据类反馈附带的论文原文证据（RAG 检索结果，可引用其中的真实内容）。

## 模板骨架与内容区的对应关系（重要）

  - 模板骨架里的 {{KICKER}}/{{TITLE}}/{{LEAD}}/{{TCAP}}/{{THEAD}}/{{TBODY}}/
    {{POINTS}}/{{SOURCE}}/{{PAGE}} 等占位符，在真实 HTML 里已被内容替换——
    你要改的就是这些**内容区**；
  - 骨架里的结构类（.slide / .table-wrap / .points / .foot 等）是**模板固定
    结构**：布局、间距、页脚位置原则上不动，除非反馈明确点名要改。

## 当前 HTML 的组件结构（选择器从这些类名里选）

  - 页头：.head > .kicker（左上小标签）、.title（页标题）
  - 要点列表：.points > .point（含 .idx 序号、.txt 文本、.src 来源标注）
  - 图片：.figure > img + .cap（图注）
  - 表格：.table-wrap > table（thead/tbody，单元格是 <th>/<td>）
  - 结论框：.callout（含 .ct 标题、.cx 正文）
  - 普通段落：.text
  - 布局容器：.box.row（左右分栏）、.box.col（上下堆叠）、.box.grid
  - 页脚：.foot > .src（论文来源）、.page（页码）

## 常用微调选择器示例

  .figure img { 调图片大小/位置 }        .points .txt { 要点字号 }
  .title { 标题字号/颜色 }                .kicker { 标签字号/颜色 }
  .point { 要点行间距 }                    .table-wrap table { 表格字号 }
  .callout { 结论框背景/内边距 }            .box.row { 分栏间隙 }
  .src { 来源标注样式 }

## 字体大小纪律（必须遵守，与 VLM 审查一致）

  - 只有反馈明确说"溢出/被裁剪/重叠/完全不可读"时才改字号，且只改被点名
    的元素，给出具体值（如 .points .txt 改为 15px）；
  - 不要因为"观感不统一/字有点小"就批量改字号；同类元素字号不一致时，
    统一对齐到模板默认值（正文 14px / 标题 28px / kicker 17px 量级），
    不要逐条自定义不同字号。

## 溢出处理（文字溢出类反馈 H2 / vlm:overflow 的必选动作）

  - 溢出反馈会带「溢出源文字」（culprit 字段或 desc 里截取的原文）：先在
    当前 HTML 里定位那段文字（replace_text 的 old 用它精确匹配原文片段）；
  - **禁止用改字号解决溢出**（set_style 改 font-size/line-height 会被系统
    拒绝——字号由模板统一控制）。只能改内容，两种修法按需选择：
      ① 压缩：把该段文字改写为更精炼的书面语句（replace_text）；
      ② 拆分：把一条过长的要点拆成两条（insert_after 插入新的
        .point/.txt 节点，复制原节点的 class 结构，保持模板样式一致）。

你必须输出一个 **JSON 数组**（补丁指令列表），每条指令是一个对象，格式如下：

[
  {"op": "set_style", "selector": ".title", "prop": "font-size", "value": "34px"},
  {"op": "set_style", "selector": ".points .txt", "prop": "color", "value": "#334155"},
  {"op": "set_attr", "selector": ".figure img", "attr": "src", "value": "assets/fig3.png"},
  {"op": "replace_text", "selector": ".point:nth-child(2) .txt", "old": "独立通道", "new": "混合通道 (Channel-mixing)"},
  {"op": "remove", "selector": ".src"},
  {"op": "append", "selector": ".box.col", "html": "<div class='callout'><div class='ct'>要点</div><div class='cx'>补充内容</div></div>"},
  {"op": "insert_after", "selector": ".figure", "html": "<div class='callout'><div class='ct'>要点</div><div class='cx'>...</div></div>"},
  {"op": "rewrite", "html": "<!DOCTYPE html>...整页..."}
]

铁律：
  - **内容必须来自论文原文或 RAG 证据**，不得编造数字、结论、方法名。
  - 优先用局部指令（set_style / replace_text / remove / set_attr / append /
    insert_after）解决；**模板页禁止使用 rewrite**（系统会拒绝并丢弃该指令）——
    模板页的布局、背景、页脚是模板固定结构，只能改内容区。
  - **禁止修改页面级结构**：不要对 .slide / body / html / .foot / .points /
    .grid / .figure-wrap / .table-wrap 等结构选择器使用 set_style / remove /
    replace_text（背景色、整体布局、页脚页码、表格容器是模板设计的一部分；
    系统会拒绝这些指令）。只改内容区选择器：.title / .kicker / .point /
    .card / .cap / .cd / .callout / .figure img / 表格的 td/th 等。
  - selector 必须**精确取自当前 HTML 源码**（class/id/标签路径），
    不要编造不存在的选择器。
  - replace_text 的 old 必须精确匹配当前 HTML 里的原文（含空格，忽略首尾空白）。
  - 修改要有针对性：反馈说缺什么就补什么，说溢出就调字号/布局，不无谓重排。
  - 不使用 <script>、内联事件、外链图片/CSS/字体（沙箱会拦）。
  - 图片只能用相对路径（assets/ 下的本地文件）。
  - replace_text 的 new **禁止包含 HTML 标签**（<span>、<b> 等），
    只写纯文本；如需防止换行/加粗，用 set_style 给选择器加样式
    （如 white-space 相关需求直接改 .src 的样式）。
  - 只输出 JSON 数组本身，不要解释、不要 markdown 代码围栏。"""

_USER_TEMPLATE = """【第 {page} 页 · {title}】
本页模板：{template}
{SKELETON}

当前 HTML 源码：
```html
{html}
```

审查反馈：
{issues}

{RAG_EVIDENCE}

请输出 JSON 补丁指令数组（不要输出 HTML 文档本身）。"""


def _issue_text(issues: list[dict]) -> str:
    if not issues:
        return "（无问题）"
    lines = []
    for it in issues:
        sev = it.get("severity") or ""
        rule = it.get("rule") or ""
        prefix = f"[{rule}{' ' if rule else ''}{'(' + sev + ')' if sev else ''}] "
        lines.append(f"- {prefix}{it.get('desc', '')}")
    return "\n".join(lines)


def _extract_html(text: str) -> str:
    """从 LLM 输出里提取 HTML（容错：markdown 围栏 / 前后解释）。"""
    text = (text or "").strip()
    m = re.search(r"```(?:html)?\s*(<!DOCTYPE html>.*?)```",
                  text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r"(<!DOCTYPE html>.*?</html>)", text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return text


#: 页面级结构选择器（模板固定结构，禁止内容级修改指令触碰）。
#: 背景色、页面布局、页脚/页码、表格容器属于模板设计，Fixer 只能在内容区
#: （.title/.kicker/.point/.card/.cap/.cd/.callout/.figure img 等）做修改，
#: 否则会重演"Fixer 把 chapter 页改成自由 HTML / 改掉全篇背景"的回归。
_STRUCT_SELECTORS = {
    "html", "body", ".slide", ".foot", ".points", ".grid", ".figure-wrap",
    ".fig-main", ".fig-side", ".table-wrap", ".mod", ".flow", ".fig-row",
    ".chapter", ".cover", ".closing", ".hero-num", ".accent", ".rule",
}

#: 结构类选择器的前缀匹配：选择器命中这些根/容器类名时按结构保护处理
_STRUCT_PREFIXES = (".slide", ".foot", ".points", ".grid", ".figure-wrap",
                    ".fig-main", ".fig-side", ".table-wrap", ".mod", ".flow",
                    ".fig-row", ".chapter", ".cover", ".closing", ".hero-num",
                    ".accent", ".rule", "body", "html")


def _is_struct_selector(selector: str) -> bool:
    """选择器是否命中模板结构（背景/布局/页脚/容器）。"""
    sel = (selector or "").strip()
    if not sel:
        return False
    # 去掉伪类/后代后取第一个选择器主体
    head = sel.split(",")[0].strip()
    head = re.split(r"[ >+~:.]", head, 1)[0]
    if head in ("html", "body"):
        return True
    # 类选择器：直接命中结构类或其后代（子选择器以结构类开头）
    for pre in _STRUCT_PREFIXES:
        if sel.startswith(pre) or sel.startswith(pre + " "):
            return True
    return False


def _apply_patch(html: str, ops: list[dict], *,
                 template_id: str | None = None) -> dict:
    """确定性应用补丁。返回 {"html", "applied": [str], "failed": [str]}。

    template_id：非空表示当前页来自确定性模板（render_* 生成）。此时：
      - 禁止 rewrite（整页重写是 Fixer 把模板页改成自由 HTML 的根因）；
      - 禁止对页面级结构选择器（.slide/.foot/.points/body/html 等）做
        set_style/remove/replace_text（背景、布局、页脚是模板固定结构）。
    模板页只能通过局部内容区指令修改，保证模板一致性不被破坏。
    """
    from lxml.html import fragment_fromstring, fromstring
    applied: list[str] = []
    failed: list[str] = []

    doc = fromstring(html)
    for op in ops:
        if not isinstance(op, dict) or "op" not in op:
            failed.append(f"非法指令: {str(op)[:60]}")
            continue
        kind = op["op"]
        try:
            if kind == "rewrite":
                if template_id:
                    failed.append(
                        "rewrite 被拒绝：当前页来自模板 "
                        f"'{template_id}'，禁止整页重写（会破坏模板一致性）。"
                        "请改用局部指令（set_style/replace_text/set_attr/append/"
                        "insert_after）只修改内容区。")
                    continue
                new_html = _extract_html(str(op.get("html", "")))
                if new_html and "<html" in new_html.lower():
                    return {"html": new_html, "applied": ["rewrite"],
                            "failed": failed}
                failed.append("rewrite 输出不是完整 HTML")
                continue

            selector = str(op.get("selector", "")).strip()
            if not selector:
                failed.append(f"{kind}: 缺 selector")
                continue
            if template_id and _is_struct_selector(selector):
                if kind in ("set_style", "remove", "replace_text"):
                    failed.append(
                        f"{kind} 被拒绝：选择器 '{selector}' 命中模板固定结构"
                        "（背景/布局/页脚/容器），模板页禁止修改结构。"
                        "请只改内容区（.title/.kicker/.point/.card/.cap/.cd/"
                        ".callout/.figure img 等）。")
                    continue
            els = doc.cssselect(selector)
            if not els:
                failed.append(f"{kind}: 选择器 '{selector}' 无匹配")
                continue

            selector = str(op.get("selector", "")).strip()
            if not selector:
                failed.append(f"{kind}: 缺 selector")
                continue
            els = doc.cssselect(selector)
            if not els:
                failed.append(f"{kind}: 选择器 '{selector}' 无匹配")
                continue

            if kind == "set_style":
                prop = str(op.get("prop", "")).strip()
                value = str(op.get("value", "")).strip()
                if not prop or not value:
                    failed.append(f"{kind}: 缺 prop/value")
                    continue
                # 字号纪律（确定性闸门）：Fixer 禁止修改字号/行高。
                # 字号由模板统一控制；确定性审查确认严重溢出
                # 时，在模板层修（压内容/改模板 class），不用内联字号。
                if prop.lower() in ("font-size", "line-height"):
                    failed.append(
                        f"{kind} 被拒绝：修改 {prop} 被禁止"
                        "（字号纪律，模板统一控制字号）。"
                        "请改用压缩内容/移除多余文字。")
                    continue
                for el in els:
                    style = el.get("style") or ""
                    # 合并：删除同名属性，再追加
                    pat = re.compile(
                        rf"(?:^|;)\s*{re.escape(prop)}\s*:[^;]*;?")
                    style = pat.sub("", style).strip(";").strip()
                    style = (style + ";" + f"{prop}:{value}") if style else f"{prop}:{value}"
                    el.set("style", style)
                applied.append(f"{kind} {selector} {prop}={value}")
            elif kind == "set_attr":
                attr = str(op.get("attr", "")).strip()
                value = str(op.get("value", ""))
                if not attr:
                    failed.append(f"{kind}: 缺 attr")
                    continue
                for el in els:
                    el.set(attr, value)
                applied.append(f"{kind} {selector} {attr}={value}")
            elif kind == "replace_text":
                old = str(op.get("old", ""))
                new = str(op.get("new", ""))
                if not old:
                    failed.append(f"{kind}: 缺 old")
                    continue
                hit = False
                for el in els:
                    if el.text and old.strip() in el.text:
                        if "<" in new and ">" in new:
                            # new 含 HTML 标签：插入真实节点，而不是把标签写进
                            # 文本（否则 lxml 序列化会把 < 转义成字面文本，
                            # 页面显示 <span ...> 源码）
                            before, after = el.text.split(old, 1)
                            el.text = before
                            frag = fragment_fromstring(new, create_parent=True)
                            nodes = list(frag)
                            for n in reversed(nodes):
                                el.insert(0, n)
                            if nodes:
                                nodes[-1].tail = (nodes[-1].tail or "") + after
                            else:
                                el.text = (el.text or "") + after
                        else:
                            el.text = el.text.replace(old, new, 1)
                        hit = True
                        break
                if hit:
                    applied.append(f"{kind} {selector} '{old}'->'{new}'")
                else:
                    failed.append(f"{kind}: '{old}' 未在 {selector} 文本中找到")
            elif kind == "remove":
                for el in els:
                    el.drop_tree()
                applied.append(f"{kind} {selector}")
            elif kind == "append":
                frag = fragment_fromstring(str(op.get("html", "")),
                                           create_parent=False)
                for el in els:
                    el.append(frag)
                applied.append(f"{kind} {selector}")
            elif kind == "insert_after":
                frag = fragment_fromstring(str(op.get("html", "")),
                                           create_parent=False)
                for el in els:
                    el.addnext(frag)
                applied.append(f"{kind} {selector}")
            else:
                failed.append(f"未知 op: {kind}")
        except Exception as e:  # noqa: BLE001
            failed.append(f"{kind} {op.get('selector', '?')}: {e}")

    from lxml.html import tostring
    return {"html": tostring(doc, encoding="unicode", method="html"),
            "applied": applied, "failed": failed}




def fix_slide_html(llm: LLMClient, html_path: Path, issues: list[dict],
                   rag_evidence: str = "", page_no: int = 0,
                   title: str = "", max_tokens: int = 8192,
                   template_hint: str = "") -> dict:
    """修复一页：LLM 输出 patch ops -> 确定性应用 -> 净化 -> 写回。

    返回 {"ok", "changed": bool, "old_len", "new_len", "removed": [...],
          "diff_head": 摘要, "patch": {"applied": [...], "failed": [...]}}

    template_hint：Router/VLM 提示的模板 ID（可空）；为空时由 detect_template
    从 HTML 特征确定性判断。骨架随 user prompt 注入，让 LLM 知道改哪里。
    """
    old_html = html_path.read_text(encoding="utf-8")
    tid = template_hint or detect_template(old_html) or ""
    skel = template_skeleton(tid)
    skel_block = ("模板骨架（去掉样式的 body 结构，{{占位符}} 即内容区）：\n"
                  + skel) if skel else "（未能识别模板骨架，请以当前 HTML 结构为准，保持整体布局不变）"
    user = _USER_TEMPLATE.format(
        page=page_no or html_path.stem,
        title=title or html_path.stem,
        template=tid or "未识别",
        SKELETON=skel_block,
        html=old_html,
        issues=_issue_text(issues),
        RAG_EVIDENCE=("论文原文证据（只引用其中的真实内容）：\n"
                      + rag_evidence if rag_evidence else "（本页无缺数据类反馈）"),
    )
    msgs = [{"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user}]
    ops: list[dict] | None = None
    last_err = ""
    import time
    for attempt in range(3):
        try:
            # max_tokens 给足：deepseek-v4-flash 是推理模型，reasoning_content
            # 可能吃掉 4K 配额导致 content=''，8K 保证 patch JSON 能落地
            ops = llm.chat_json(msgs, temperature=0.1, max_tokens=8192)
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
            ops = None
        if isinstance(ops, list) and ops:
            break
        if ops == []:
            break  # LLM 明确判定无需修改（合法结果，非失败）
        if attempt < 2:
            time.sleep(2 * (attempt + 1))

    if ops == []:
        # LLM 判定该页无需修改：ok=True / changed=False（router 会记“未变化”）
        return {"ok": True, "changed": False, "error": "",
                "old_len": len(old_html), "new_len": len(old_html),
                "removed": [], "diff_head": [], "patch": {"applied": [], "failed": []}}
    if not isinstance(ops, list) or not ops:
        print(f"[HTMLFixer] 第{page_no}页 LLM 未输出有效补丁"
              + (f"（{last_err}）" if last_err else ""))
        return {"ok": False, "error": last_err or "LLM 未输出有效补丁",
                "changed": False}

    result = _apply_patch(old_html, ops, template_id=tid)
    new_html = result["html"]
    # 沙箱净化后落盘
    cleaned = sanitize_html(new_html, assets_dir=html_path.parent.parent / "assets")
    html_path.write_text(cleaned["html"], encoding="utf-8")

    changed = cleaned["html"].strip() != old_html.strip()
    # 被保护拦截的指令（模板结构保护）：若全部指令被拒且页面未变化，
    # 说明 LLM 试图越权改结构——在 diff 里显式记录，避免被当成"无需修改"
    rejected = [f for f in result["failed"]
                if "被拒绝" in f and ("模板" in f or "结构" in f)]
    if rejected and not changed:
        result["failed"] = rejected
    # diff 摘要：逐行差异前 5 处
    diff_lines = []
    old_lines = old_html.splitlines()
    new_lines = cleaned["html"].splitlines()
    for i, (a, b) in enumerate(zip(old_lines, new_lines)):
        if a != b and len(diff_lines) < 5:
            diff_lines.append(f"L{i+1}: {a[:60].strip()} -> {b[:60].strip()}")
    return {
        "ok": True,
        "changed": changed,
        "old_len": len(old_html),
        "new_len": len(cleaned["html"]),
        "removed": cleaned["removed"],
        "diff_head": diff_lines,
        "patch": {"applied": result["applied"], "failed": result["failed"]},
    }


def fix_slide_with_rag(llm: LLMClient, html_path: Path, issues: list[dict],
                       rag_search, page_no: int = 0, title: str = "",
                       top_k: int = 3, template_hint: str = "") -> dict:
    """带 RAG 的修复入口：缺数据类 issue 先检索论文，证据随反馈一并给 LLM。

    rag_search：callable(query, top_k) -> [evidence,...]（注入依赖，便于测试）。
    """
    missing = [it for it in (issues or [])
               if it.get("_bucket") == "missing_data"]
    rag_evidence = ""
    if missing:
        from .router import build_rag_query
        queries = [build_rag_query(it) for it in missing]
        evidence = []
        for q in queries:
            evidence.extend(rag_search(q, top_k=top_k))
        rag_evidence = format_evidence(evidence, max_chars=3000)
    return fix_slide_html(llm, html_path, issues, rag_evidence,
                          page_no=page_no, title=title,
                          template_hint=template_hint)
