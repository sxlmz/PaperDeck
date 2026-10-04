# PatchTST 学术风 PPT 模板

## 整体风格

- **背景**：统一浅米灰 `#F5F5F0`
- **说明卡**：纯白 `#FFFFFF` + 左侧 4px 蓝/绿色条
- **主色**：标题黑 `#0F172A`，正文 `#1E293B`，次要文字 `#64748B`
- **强调色**：6 色循环
  - `#2563EB` 蓝
  - `#16A34A` 绿
  - `#EA580C` 橙
  - `#7C3AED` 紫
  - `#0891B2` 青
  - `#DB2777` 玫
- **字体**：Microsoft YaHei 微软雅黑，正文 14pt
- **画布**：1280×720 px（16:9），导出 13.333"×7.5"

## 模板清单与选择规则

> **强制规则：表格和图片模板严格分开。**
> - 出现数据表格 → 只能选 **表格类模板**（table_one / table_notes_v/h / dual_table_summary / dual_table_notes_v/h）
> - 出现图表/图片 → 只能选 **图表类模板**（chart_notes_one/v/h / dual_chart_summary/v/h）
> - 严禁在图表模板里塞表格，也严禁在表格模板里塞图表图片。
>
> **图注/表注规则（所有图表/表格模板通用）：**
> - 图表/表格的图注（如"图 1：MSE 对比"、"表 1：数据集统计"）→ 填在 `{{CAPTION}}` / `{{CAPTION1}}` / `{{CAPTION2}}` 变量里，位于图表区域下方
> - **所有说明卡（NOTE / NOTE_1 / NOTE_2 / SUMMARY / 右侧卡片）里绝对不允许写图注/表注**，说明卡只写总结性结论或对图表的解读

---

### 封面与章节

#### cover.html — 封面
- 浅米灰底 + 底部三色波形装饰 + 蓝色短横线
- 期刊名（蓝色小字）+ 大标题 + 副标题
- 变量：`VENUE`（如 ICLR 2023）、`TITLE`、`SUBTITLE`

#### toc.html — 汇报大纲（目录页）
- **固定放在第 2 页**，只用于介绍全文分四个模块讲解，**只出现一次**
- Contents 小字 + 大标题 + 蓝短横线 + 2×2 四模块
- 大号彩色序号（蓝/绿/橙/灰）+ 模块标题 + 一句话说明
- 变量：`TITLE`（如"汇报大纲"）、`M1_TITLE`/`M1_DESC`、`M2_TITLE`/`M2_DESC`、`M3_TITLE`/`M3_DESC`、`M4_TITLE`/`M4_DESC`

#### chapter.html — 过渡章节页
- 浅米灰底 + 右侧淡折线装饰 + 蓝色短横线
- Chapter 编号 + 大标题 + 副标题
- 变量：`CHAPTER_NUM`（如 04）、`TITLE`、`SUBTITLE`

---

### 单图表类（有图片/折线图/柱状图时选）

| 模板 | 说明条数 | 说明排布 |
|---|---|---|
| `chart_notes_one.html` | 1 | 单条总结 |
| `chart_notes_v.html` | 2 | 上下排列 |
| `chart_notes_h.html` | 2 | 左右并排 |

- 变量通用：`KICKER` / `TITLE` / `SUBTITLE` / `CHART_TITLE` / `CHART`
- one 版：`NOTE`
- v/h 版：`NOTE_1` / `NOTE_2`（蓝/绿竖条）

---

### 单表格类（booktabs 三线表）

**有数据表格时，从以下三个模板中选一个：**

| 模板 | 说明条数 | 说明排布 |
|---|---|---|
| `table_pretty.html` | 1 | 单条总结 |
| `table_notes_v.html` | 2 | 上下排列 |
| `table_notes_h.html` | 2 | 左右并排 |

- 表格风格：学术三线表（顶/中/底黑横线，无竖线），数字列蓝色斜体
- 变量通用：`KICKER` / `TITLE` / `SUBTITLE` / `TCAP` / `THEAD` / `TBODY`
- one 版：`SUMMARY`
- v/h 版：`NOTE_1` / `NOTE_2`（蓝/绿竖条）

---

### 双图片对比类

**涉及两张图片对比时，从以下三个模板中选一个：**

| 模板 | 说明条数 | 说明排布 |
|---|---|---|
| `dual_chart_summary.html` | 1 | 单条总结 |
| `dual_chart_v.html` | 2 | 上下排列 |
| `dual_chart_h.html` | 2 | 左右并排 |

- 变量：`KICKER` / `TITLE` / `SUBTITLE` / `CHART1_TITLE` / `CHART1` / `CHART2_TITLE` / `CHART2`
- summary 版：`NOTE`
- v/h 版：`NOTE_1` / `NOTE_2`

---

### 双表格对比类

**涉及两张表格对比时，从以下三个模板中选一个：**

| 模板 | 说明条数 | 说明排布 |
|---|---|---|
| `dual_table_summary.html` | 1 | 单条总结 |
| `dual_table_notes_v.html` | 2 | 上下排列 |
| `dual_table_notes_h.html` | 2 | 左右并排 |

- 变量：`KICKER` / `TITLE` / `SUBTITLE` / `TABLE1_TITLE` / `THEAD1` / `TBODY1` / `TABLE2_TITLE` / `THEAD2` / `TBODY2`
- summary 版：`NOTE`
- notes v/h 版：`NOTE_1` / `NOTE_2`

---

### 左图/表 + 右侧多卡说明类

**只有在「有图片/表格」且「要说明的文字有 2 点以上」时才用这两个模板。**

| 模板 | 左侧内容 | 右侧卡片数 |
|---|---|---|
| `left_right_chart.html` | 单张图表 | 2-4 张，flex 上下排列 |
| `left_right_table.html` | 单张表格（三线表） | 2-4 张，flex 上下排列 |

**严格规则：**
- 右侧文字卡片 **最少 2 张、最多 4 张**，不允许 1 张（1 张用 chart_notes_one / table_one），也不允许 5 张及以上
- 如果说明文字 **超过 4 点，必须让 LLM 拆成两页 PPT**，每页最多 4 张卡，禁止硬塞一页
- 卡片样式同 `conclusion_grid`：白底 + 左色条（蓝/绿/橙/紫）+ 彩色序号 + 标题 + 说明
- **图注/表注（如"表 3：..."、"图 1：..."）必须填在左侧的 `CAPTION` 变量里，位于图表/表格下方**
- **右侧卡片里绝对不允许写图注/表注**，右侧卡片只写总结性结论/对图表的解读

---

### 纯文本类（不允许出现任何图表/表格）

**这类模板只允许有文字，不允许插入任何图表、图片、表格。**

| 模板 | 适用场景 | 条目数限制 |
|---|---|---|
| `flow_1x4.html` | 横向流程卡 / 最多不超过四点 | **最多 4 点，横向并排** |
| `conclusion_grid.html` | 超过四点的文本（骰子卡片） | **1-6 点，flex骰子布局** |

**规则：**
- 如果文本分点 **不超过 4 点**，可以在 `flow_1x4.html`和`conclusion_grid.html`选择其中一个
- 如果文本分点 **超过 4 点**，必须用 `conclusion_grid.html`

---

## 导出方式

使用 dom-to-pptx：
```bash
npx dom-to-pptx-exporter slides.html --output presentation.pptx --width 13.333 --height 7.5
```
