# 指标口径定义

> 口径必须能从原始数量重算出来，否则不写进看板。本文所有定义都对应
> `sql/11_mes_dwd.sql` / `sql/13_mes_ads.sql` 中的实现，不是文案描述。

## 1. 良率

| 指标 | 定义 | 实现位置 | 说明 |
| --- | --- | --- | --- |
| 工序良率 `step_yield` | `output_qty / input_qty` | `dwd_mes_process` | 每批 × 每工序一行，来自原始数量，不读合成良率列 |
| 批次最终良率 `yield_final` | `末道工序 output / 首道工序 input` | `dwd_mes_lot_route` | 与 `lots.yield_final` 校验一致（`is_qty_consistent`） |
| 线体日良率 | `SUM(good_qty) / SUM(input_qty)`，按线 × 日 | `dws_mes_line_daily` → `ads_mes_kpi_daily` | 加权，不是日良率的算术平均 |
| 全线日良率 | 当日所有线 `SUM(good_qty) / SUM(input_qty)` | 看板 `overview.kpis.yield_pct` | 同上，加权口径 |
| 7 日均良率 | 当日之前 7 天的 `yield_final` 均值 | `ads_mes_kpi_daily.yield_7d_avg` | 窗口 `ROWS BETWEEN 7 PRECEDING AND 1 PRECEDING`，**不含当日**，避免自我比较 |
| 良率变化 `delta_pp` | `(当日良率 - 前 7 日均值) × 100` | 同上 | 单位 pp（百分点），不是相对变化 % |

### 1.1 FPY 与 RTY，本项目怎么用

- **FPY（First Pass Yield）**：一次通过率。本仓库的 `step_yield` 在语义上等价于单工序 FPY。
- **RTY（Rolled Throughput Yield）**：各工序 FPY 连乘，等于 `yield_final`（数量守恒下严格相等）。
- 看板标题用「良率」，正文用「工序良率 / 最终良率」，**不使用未定义的 FPY 缩写**，避免与客户口径混淆。

### 1.2 不计算 OEE

OEE = 可用率 × 性能 × 质量，需要停机时长、节拍、理论产能。当前数据没有停机和节拍字段，
因此**不提供 OEE**，也不做估算。若接入 MES 的 `equipment_downtime` 与 `cycle_time`，
可在 DWS 增加一层设备日汇总，质量因子复用本文良率口径。

## 2. 不良与损失

| 指标 | 定义 | 说明 |
| --- | --- | --- |
| 工序不良数 | `input_qty - output_qty` | 与 `defect_qty` 一致，守恒校验失败会 fail 门禁 |
| 不良率 | `defect_qty / input_qty` | 看板下钻「日不良率趋势」 |
| 损失构成 | 按 `process_code × defect_code` 汇总近 7 天不良数 | `ads_mes_loss_pareto`，按日 `RANK()` 后聚合 |
| 损失占比 | 单项不良 / 全部不良 | 看板总览横向条形图 |

## 3. SPC 控制图

| 项 | 取值 | 说明 |
| --- | --- | --- |
| 序列粒度 | `线 × 工序 × 设备 × 参数` 的**小时均值** | 小时样本数 < `min_samples_per_hour`(2) 的小时被剔除 |
| 基线窗口 | 前 `baseline_days = 60` 天 | 基线冻结后不再回算，避免异常污染控制限 |
| 中心线 `base_mean` | 基线小时均值的均值 | |
| 标准差 `base_sd` | 基线**小时均值**的标准差 | 不是小时内的 pooled sd；后者会把正常波动放大成误报 |
| 控制限 | `base_mean ± 3 × base_sd` | 双侧 |
| 标准化 `z` | `(小时均值 - base_mean) / base_sd` | |
| 最少基线小时数 | 24 | 不足 24 小时不建控制限，不出告警 |

### 3.1 判异规则

| 规则 | 代码 | 触发条件 | 严重度 |
| --- | --- | --- | --- |
| 3σ 连续越界 | `R1_3SIGMA` | 连续 `sustained_points = 3` 个点 `|z| ≥ 3` | high |
| 连续 7 点偏移 | `R2_SEVEN_POINT` | 连续 `run_length = 7` 点同侧，且这 7 点均值偏移 `≥ run_mean_sigma = 2σ` | medium |

判定是**单次触发**：每条序列每条规则只保留第一次命中的时间点，避免同一漂移刷出几十条告警。
`z_score` 存的是触发点的统计量：R1 为第 3 个越界点的 z，R2 为 7 点 run mean 的 z。

### 3.2 线体状态

`ads_mes_kpi_daily.status` 的判定顺序：

1. 当日存在告警，或 `delta_pp ≤ -0.5` → `alert`
2. 否则 `delta_pp ≤ -0.2` → `watch`
3. 否则 `normal`

阈值为工程约定，写在 `src/warehouse/mes.py`，不是统计推断结果。

## 4. 可疑批次

| 项 | 定义 |
| --- | --- |
| 回溯窗口 | 告警时间前 `suspect.lookback_hours = 8` 小时 |
| 匹配条件 | 同设备 + 同工序 |
| 排序 | 不良数量降序，每个告警取 Top 20 |
| 用途 | 给工艺/设备工程师的复判清单，不是「已确认不良」 |

## 5. 场景验证（注入异常 ↔ 告警一致性）

每条注入剧本在 `ads_mes_scenario_check` 中给出 5 项 check，全部为真才算 `MATCH`：

| check | 判定 |
| --- | --- |
| `alert` | 该序列至少有一条告警 |
| `rule` | 首次告警的 `rule_code` 与剧本 `expected_rule` **严格相等** |
| `step_drop` | 剧本窗口内工序良率相对基线下降 ≤ `-0.8pp` |
| `final_drop` | 剧本窗口内最终良率相对基线下降 ≤ `-0.5pp` |
| `directed` | 首次告警相对漂移开始的滞后 ≤ `max(24h, ramp+12h)` |

窗口口径：工序/最终良率的「窗口值」取窗口**最后 24 小时的峰值下降**（不是全窗口均值），
因为缓慢漂移的均值会被前期正常数据稀释；原因字段落在 `detail` JSON 中，可用
`python -m src.cli scenario` 复现打印。

**已知短板（不隐藏）**：缓慢漂移（S2，240h ramp）中，工序良率可能在参数首次越 3σ 之前就开始下降
——因为良率对连续漂移是连续响应，而 3σ 只是一个报警阈值。S2 的「参数越 3σ」晚于「工序良率首降」，
这正是 R2 连续 7 点规则存在的理由，也是慢漂移检测滞后的真实体现（S2 检出滞后 51h，S1 为 5h）。

## 6. 数据质量门禁

| 家族 | MES 检查 | 失败后果 |
| --- | --- | --- |
| 完整性 | `mes_lot_row_count`、`mes_step_row_count`、`mes_time_continuity` | fail，阻断 |
| 有效性 | `mes_step_yield_domain`、`mes_quantity_conservation`、`mes_route_yield_parity` | fail，阻断 |
| 一致性 | `mes_kpi_caliber_consistency`（ADS 与 DWS 良率差 < 1e-6） | fail，阻断 |
| 可追溯 | `mes_scenario_detected`（MATCH 数 = 剧本数）、`mes_alert_traceability`（未追溯告警数）、`mes_source_labelled`（合成声明） | fail / warn |

完整报告见 `artifacts/quality_report.json`。
