# 数据字典

## 1. 数据源

### SECOM

| 项 | 内容 |
| --- | --- |
| 来源 | UCI Machine Learning Repository, id 179 |
| 直链 | `https://archive.ics.uci.edu/static/public/179/secom.zip` |
| 许可 | CC BY 4.0 |
| 文件 | `secom.data`（空格分隔，无表头）、`secom_labels.data`（标签 + 时间戳） |
| 规模 | 1,567 行 × 590 传感器；104 片失效（6.64%） |
| 标签 | `-1` = pass，`1` = fail |
| 版本锁定 | `archive_sha256: EEA568BAF3C2229096D7D294CF0B096B5502BD96D92C0B80A65B84714059BE8E` |

### WM-811K（派生特征子集）

| 项 | 内容 |
| --- | --- |
| 原始数据 | WM-811K LSWMD.pkl，811,457 张晶圆图，9 类缺陷，CC0 |
| 本项目输入 | 公开镜像 `ililllilll/WM811K/data/wm811k_labeled.npz`（26 维特征，172,950 行） |
| 许可 | CC0（原始 WM-811K） |
| 划分 | 官方 `Training` / `Test`：54,355 / 118,595 |
| 版本锁定 | `npz_sha256: 95CDCC455C4D3E1FD3AC28D9A237DBC0C2BEE94324C8E1384A17A73360A6EE50` |
| 取样策略 | 使用该派生表全部有效行；不重新随机切分；不使用原始图像 |

26 维特征：`rows, cols, total_grid, total_die, fail_die, wafer_yield, defect_density, center_defect_ratio, mid_defect_ratio, edge_defect_ratio, edge_to_center_ratio, quadrant_imbalance, left_defect_ratio, right_defect_ratio, top_defect_ratio, bottom_defect_ratio, defect_centroid_x, defect_centroid_y, defect_spread_x, defect_spread_y, radial_mean, radial_std, cluster_count, largest_cluster_ratio, die_size, wafer_index`。

> DWD 层剔除 `total_grid`（`rows × cols` 的冗余列），模型实际使用 25 维。

## 2. 分层表结构

### ODS（原始层）

| 表 | 粒度 | 行数 | 说明 |
| --- | --- | --- | --- |
| `ods_secom_sensor_value` | 晶圆 × 传感器 | 924,530 | 长表；主键 `(wafer_id, sensor_id)`；保留原始值与原始缺失 |
| `ods_wm811k_wafer` | 晶圆图 | 172,950 | 26 维特征 + 标签 + 官方划分 |

### DWD（清洗层）

| 表 | 粒度 | 行数 | 说明 |
| --- | --- | --- | --- |
| `dwd_wafer_label` | 晶圆 | 1,567 | `wafer_id, measurement_ts, stat_date, is_fail, split` |
| `dwd_sensor_value` | 晶圆 × 传感器 | 924,530 | 增加 `is_missing`、`is_imputed`、`is_outlier` 标记；中位数填充与 ±6σ 缩尾 |
| `dwd_sensor_profile` | 传感器 | 590 | 缺失率、唯一值数、均值/中位数/标准差、`is_constant`、`is_high_missing`、`is_usable` |
| `dwd_wm811k_wafer` | 晶圆图 | 172,950 | 行级合法性校验（`is_valid` / `invalid_reason`） |

### DWS（汇总层）

| 表 | 粒度 | 行数 | 说明 |
| --- | --- | --- | --- |
| `dws_wafer_summary` | 晶圆 | 1,567 | 缺失数、填充数、异常数、缺失率、异常率 |
| `dws_sensor_daily` | 传感器 × 日 | 37,496 | 日均值/标准差/P05/P95/缺失率，用于漂移监控 |
| `dws_defect_class_dist` | 缺陷类别 | 9 | 类别计数与占比 |
| `dws_wm811k_split` | 官方划分 × 类别 | 18 | 训练/测试分布 |

### ADS（应用层）

| 表 | 粒度 | 行数 | 说明 |
| --- | --- | --- | --- |
| `ads_training_manifest` | 训练运行 | 1 | 特征列表（JSON）、切分边界、样本数 |
| `ads_yield_feature_matrix` | 晶圆 | 1,567 | 440 维特征宽表 + `split` + `is_fail` |
| `ads_wafer_risk` | 晶圆 | 1,567 | 失效概率、风险档位、预警标记、Top 3 驱动参数及贡献 |
| `ads_yield_trend_daily` | 日 | 86 | 实际良率、测试期平均风险、预警数 |
| `ads_sensor_drift` | 传感器 × 日 | 37,496 | 相对训练基线的 z-score、±3σ 控制线、是否漂移 |
| `ads_defect_class_perf` | 缺陷类别 | 9 | Precision / Recall / F1 / Support |
| `ads_defect_confusion` | 真实 × 预测 | 81 | 9×9 混淆矩阵 |

### 元数据表

| 表 | 说明 |
| --- | --- |
| `meta_pipeline_run` | 运行 ID、配置指纹、数据指纹、状态、时间 |
| `meta_pipeline_step` | 每步状态、行数、耗时；用于断点续跑 |
| `meta_quality_check` | 质量检查结果明细 |

## 3. 关键字段口径

| 字段 | 口径 |
| --- | --- |
| `is_fail` | 1 = 失效（原始标签 `1`），0 = 合格（原始标签 `-1`） |
| `stat_date` | `measurement_ts` 的日期部分（UTC，`YYYY-MM-DD`） |
| `split` | 按时间排序后的 `train` / `valid` / `test`，只用于离线评估 |
| `missing_rate` | 该晶圆 590 个传感器中原始缺失的比例 |
| `abnormal_rate` | 缩尾后仍超出 ±6σ 的比例 |
| `is_constant` | 该传感器在训练期内唯一值 < 2 |
| `is_high_missing` | 训练期缺失率 > 40% |
| `is_usable` | 非恒定且非高缺失，共 436 个 |
| `fail_probability` | 选中模型输出的失效概率 |
| `risk_band` | `high ≥ 0.60`，`medium ≥ 0.30`，其余 `low` |
| `alert_flag` | `fail_probability ≥ 冻结阈值 0.1416` |
| `z_score` | `(日均值 − 训练期均值) / 训练期标准差`，用于 3σ 漂移判断 |

## 4. 数据质量规则（17 项，全部 PASS）

| 检查 | 观测值 | 阈值/规则 |
| --- | --- | --- |
| `secom_wafer_row_count` | 1,567 | 等于配置的 `expected_rows` |
| `secom_sensor_column_count` | 590 | 等于 `expected_sensors` |
| `duplicate_wafer_id` | 0 | 必须为 0 |
| `label_domain` | 0 | 非法标签数必须为 0 |
| `timestamp_parse` | 0 | 无法解析的时间戳必须为 0 |
| `label_fail_rate` | 0.0664 | 落在 [0.01, 0.30] |
| `sensor_max_missing_rate` | 0.9119 | 记录最大值，用于判断是否需要剔除 |
| `constant_sensor_count` | 116 | 记录数量 |
| `high_missing_sensor_count` | 32 | 记录数量 |
| `usable_sensor_count` | 436 | 必须 ≥ 50 |
| `nonfinite_sensor_values` | 0 | 必须为 0 |
| `layer_row_parity` | ODS = DWD = 924,530 | 两层行数必须一致 |
| `wm811k_row_count` | 172,950 | 等于配置的 `expected_rows` |
| `wm811k_class_count` | 9 | 等于配置的类别数 |
| `wm811k_invalid_rows` | 0 | 无效行必须为 0 |
| `temporal_split_order` | train_max 2008-09-26 < test_min 2008-10-05 | 严格时间有序 |
| `split_sizes` | 1,097 / 235 / 235 | 与配置比例一致 |

报告输出：`artifacts/quality_report.json`，同时写入 `meta_quality_check`。
