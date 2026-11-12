# MES 数据契约与真实数据替换路径

> 本仓库主演示使用 **100% 合成数据**。真实产线数据受保密协议（NDA）限制无法获取，
> 因此没有任何一行数据来自真实工厂。本文定义合成数据与真实 MES/EAP 导出**完全一致**的
> 列契约：只要按本文导出两个 CSV，把 `mes.source` 从 `synthetic` 改成 `export`，
> 下游 DWD / DWS / ADS / SPC / 看板代码一行都不用改。

## 1. 粒度

| 文件 | 粒度 | 主键 | 合成规模（当前配置） |
| --- | --- | --- | --- |
| `lots.csv` | 一个生产批次一行 | `lot_id` | 81,000 行 |
| `process.csv` | 批次 × 工序一行 | `lot_id + process_seq` | 486,000 行 |

即：3 条线 × 90 天 × 3 班次 × 100 批/班 = 81,000 批；每批 6 道工序 = 486,000 行。

## 2. 列契约

### 2.1 `lots.csv`

| 列 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `lot_id` | string | 是 | 批次号，全局唯一，例如 `B20261109N001` |
| `line_code` | string | 是 | 线体，例如 `LINE-A` |
| `product_code` | string | 是 | 产品，例如 `PROD-01` |
| `start_shift` | string | 是 | 投料班次：`DAY` / `SWING` / `NIGHT` |
| `lot_start_ts` | ISO-8601 string | 是 | 批次投料时间 |
| `lot_size` | int | 是 | 投料数量（die 数） |
| `good_qty` | int | 是 | 最终良品数 |
| `yield_final` | float | 是 | 批次良率 = `good_qty / lot_size` |

### 2.2 `process.csv`

| 列 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `lot_id` | string | 是 | 关联 `lots.csv` |
| `line_code` / `product_code` | string | 是 | 冗余维度，便于分区裁剪 |
| `process_seq` | int | 是 | 工序顺序，从 1 开始，必须连续 |
| `process_code` | string | 是 | 工序代码，需与 `configs/config.yaml` 的 `mes.processes[].code` 一致 |
| `equipment_id` | string | 是 | 设备号，例如 `EQ-DA-02` |
| `process_ts` | ISO-8601 string | 是 | 该工序完成时间 |
| `shift_code` | string | 是 | `DAY` / `SWING` / `NIGHT` |
| `input_qty` | int | 是 | 本工序投入 |
| `output_qty` | int | 是 | 本工序产出 |
| `defect_qty` | int | 是 | 本工序不良，必须满足 `input = output + defect` |
| `defect_code` | string | 是 | 缺陷代码，例如 `VOID` / `SCRATCH` |
| `param_1` … `param_5` | float | 否 | 该工序的过程参数，位置含义见第 3 节 |

### 2.3 数量守恒（DWD 强校验）

1. `input_qty = output_qty + defect_qty`（同一批次同一工序）；
2. 第 `k` 道工序的 `output_qty` = 第 `k+1` 道工序的 `input_qty`；
3. `lots.good_qty` = 末道工序 `output_qty`。

任一不满足都会写入 `is_qty_consistent = 0`，质量门禁 `mes_quantity_conservation` /
`mes_route_yield_parity` 会直接 fail（不是静默丢弃）。

## 3. 参数位置（`param_1..param_5`）

`param_N` 是**位置参数**，按 `configs/config.yaml` 中该工序 `params` 列表的顺序绑定，
绑定关系落在 `dwd_mes_param_spec`：

```
process_code | param_index | param_code | param_name | unit | base_value | sd_value | is_key | owner_role
```

- `is_key = true` 的参数才进入 SPC 与预警（当前每道工序 2 个）；
- 真实导出如果参数更多，扩展 `param_6+` 需要同步修改
  `src/synthetic/mes.py`、`sql/10_mes_ods.sql`、`sql/11_mes_dwd.sql` 与 `PROCESS_COLUMNS`
  —— 这是当前数据模型的已知边界；
- 真实导出如果参数名不同，只需改 `mes.processes[].params[]` 的 `code/name/unit/base/sd`，
  代码逻辑不变。

## 4. 替换成真实数据的步骤

```bash
# 1) 把 MES/EAP 导出落到约定目录（默认 data/raw/mes_export/）
#    data/raw/mes_export/lots.csv
#    data/raw/mes_export/process.csv   （UTF-8，带表头）

# 2) 改数据源开关
#    configs/config.yaml
#    mes:
#      source: export
#      export_dir: data/raw/mes_export

# 3) 重跑数仓与看板
python -m src.cli warehouse
python -m src.cli quality
python -m src.cli dashboard
```

也可以复制一份配置（`configs/config.real.yaml`），用
`python -m src.cli --config configs/config.real.yaml warehouse` 指定，保留演示配置不动。

### 4.1 切换后哪些东西会变

| 项目 | `synthetic` | `export` |
| --- | --- | --- |
| 数据来源 | `src/synthetic/mes.py` 固定种子生成 | 读取 `lots.csv` / `process.csv` |
| `scenario_id` | 注入剧本 ground truth，非空 | 全部为空（真实数据没有 ground truth） |
| 场景验证 `ads_mes_scenario_check` | 2 条，均 MATCH | 0 条，`python -m src.cli scenario` 只报告警数 |
| 合成声明 | 看板顶部横幅 | 看板顶部横幅仍显示，文案需按实际情况改为「真实导入」 |
| SPC / 告警 / 下钻 / 质量门禁 | 完整 | 完整，逻辑不变 |

> 重要：真实数据模式**没有** `scenario_id`，因此「注入异常 ↔ 告警」的一致性验证天然为空。
> 演示时请保持 `source: synthetic`；真实接入时用告警队列 + 可疑批次做人工复核。

## 5. 从 MES 导出的参考 SQL

不同 MES 厂商表名不同，核心是把「批次表 + 工序过站表」映射到上面的列。示意：

```sql
SELECT
    l.lot_id,
    l.line_code,
    l.product_code,
    l.start_shift,
    l.lot_start_ts,
    l.lot_size,
    l.good_qty,
    l.good_qty * 1.0 / l.lot_size AS yield_final
FROM mes_lot l;

SELECT
    p.lot_id, p.line_code, p.product_code, p.process_seq, p.process_code,
    p.equipment_id, p.process_ts, p.shift_code,
    p.input_qty, p.output_qty, p.input_qty - p.output_qty AS defect_qty,
    p.defect_code,
    p.temperature AS param_1, p.pressure AS param_2, p.speed AS param_3,
    p.force AS param_4, p.offset AS param_5
FROM mes_process_history p;
```

## 6. 已知限制（如实说明）

- 只覆盖**封装测试** 6 道工序，不是前道晶圆制造的全流程；
- 参数只保留每道工序 5 个，真实机台通常数百个；
- 班次模型固定为 8 小时三班，不含交接班停机；
- 不计算 OEE（缺停机时长与节拍数据），只计算良率口径，见
  [metric_definitions.md](metric_definitions.md)；
- 真实数据接入后，SPC 基线需要用该产线自己的历史窗口重新冻结（默认前 60 天）。
