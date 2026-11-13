# field-ledger

读取设备测量快照，保留可空读数和单位，不对缺失值填零。结构版本、字段身份和兼容规则都落实在真实读写路径上。

## 数据组织

当前平面视图仍是：

- `measurements`：`sample`（主键）、`device`、`temperature`（可空）、`unit`。

`examples/source.json` 是本地合成输入，`examples/expected.json` 是当前查询的公开样例结果。`schema.json` 描述基线结构，基线身份在 `warehouse/definition.py` 派生。结构规则和比较位于 `warehouse/schema.py`，持久登记与审计位于 `warehouse/registry.py`，事务装载位于 `warehouse/catalog.py`，版本投影位于 `warehouse/projection.py`，读取入口位于 `warehouse/domain.py`，内容寻址 JSONL 对象位于 `warehouse/store.py`。

## 结构演进

每个字段有永久 `field_id`，嵌套路径上的每一段也各有身份。文件按写者版本登记，行内值以身份路径存储而不是列名，因此：

- 改名保留身份；旧文件投影到新版本时按身份取值，不按名字猜。
- 删除字段会退役其身份；之后同名新字段必须使用新身份，旧值不会流入复用名字。
- 新增可空字段、改名、整数到精确十进制的扩大可兼容提交。收窄类型、可空改非空、重用退役身份，以及删除后重新使用同名但沿用旧身份，都会被阻止并留下审计行。
- 默认值在版本发布时固化，读取时直接取自已登记版本，不随读取时间变化。缺失、显式 `null`、默认值三种情况在投影中保持可区分。

### 版本登记

```bash
python3 -m warehouse versions --db /tmp/field-ledger.sqlite3
python3 -m warehouse register --db /tmp/field-ledger.sqlite3 --input new-definition.json --actor alice
python3 -m warehouse view --db /tmp/field-ledger.sqlite3 --version 2
```

登记以 `BEGIN IMMEDIATE` 串行化。并发登记时只有一个成功，其余返回稳定的 `stale_base`（带当前版本/结构标识）并写入审计。调用方可带 `--base-version` / `--base-schema-id` 断言所依据的版本。

### 写者版本校验

生产者先用 `--writer-version`（或 `--writer-schema-id`）声明版本；提交时在写事务内重新校验。若期间结构已升级：

- 改名等兼容变更：旧写者的身份仍在，文件正常提交并投影。
- 写者依赖的字段被删除（身份退役）：返回 `incompatible_writer_version`，文件不进入 `stored_rows` 与 `ingest_batches`，只留下 `ingest_batches_audit` 的拒绝记录。
- 未登记或高于当前的写者版本：返回 `unknown_version`。

`--writer-version` 缺省时使用当前版本。

## 读取入口

- `inspect`：现有平面读取，当前名称、当前值、按键排序，行为与升级前一致。
- `view`：按版本查看。展示每个批次的写入版本、投影结果、目标字段来源（carried/default/missing_as_null）、结构操作（含同名复用与嵌套改名）以及审计和身份台账。
- SQL 平面表 `measurements` 作为派生表保留，随当前版本重建，供既有 SQL 调用继续使用。

## 环境与验证

Python 3.11 以上，仅使用标准库，无第三方安装。`runtime.txt` 记录验证解释器版本。

```bash
python3 -m compileall -q warehouse tests scripts
python3 -m unittest discover -s tests -v
```

测试覆盖重启后从冷进程投影历史文件（`tests/fixtures/projection_probe.py` 用独立解释器执行两个阶段）、旧平面库升级、同名复用、嵌套改名、写者版本被拒以及并发登记冲突审计。

## 运行

```bash
python3 scripts/demo.py
```

演示脚本在全新临时目录执行真实导入、重复导入、查询、导出、结构登记、旧版本写者提交、被拒绝的收窄登记和版本视图。失败返回非零退出码，不回放预制日志。

分步调用：

```bash
python3 -m warehouse import --db /tmp/field-ledger.sqlite3 --input examples/source.json --batch first
python3 -m warehouse inspect --db /tmp/field-ledger.sqlite3
python3 -m warehouse export --db /tmp/field-ledger.sqlite3 --objects /tmp/field-ledger-objects
```

旧平面数据库（`measurements` 带 `sample, device, temperature, unit`）在首次打开时被一次性采纳为版本 1 数据；无法识别的旧列会报 `legacy_table_mismatch`，不会被静默解释。
