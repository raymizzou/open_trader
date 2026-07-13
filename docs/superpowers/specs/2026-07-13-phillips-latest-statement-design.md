# 辉立最新结单设计

辉立数据以项目内 `data/statements/phillips/` 中日期最新的真实 PDF 为准，不要求固定月份。当前结单日期为 2026-07-10；证券市值 HK$573,500.27，现金折港币 HK$55,053.79，总资产 HK$628,554.06。

导入继续复用现有 `import-statements` 流程。解析器忽略数量和市值均为零的已清仓记录，并优先使用结单 Account Details 中的 `HKD(Base)` 权威现金合计，避免人工汇率改变结单原值。PDF 属于运行数据，保留在已被 Git 忽略的 `data/` 下，不提交。

验收不再固定辉立行数。它读取最新一次辉立导入的 manifest，确认来源是项目归档中的最新 PDF，并核对 Dashboard 的辉立结单日期与解析所得总资产。最后仍以 `make acceptance` 的 PASS 为唯一完成条件。
