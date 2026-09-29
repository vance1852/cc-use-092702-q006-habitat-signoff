# 形成栖息地评估的版本结论与会签基础平台

本项目是一套可离线运行的 Python 服务端平台，供国家森林公园管理局、保护站、生态监测人员和巡护队管理野生动植物观察、采样标本、保护站资源、巡护路线、风险告警与处置工单。业务状态、角色权限、幂等结果和审计事件保存在 SQLite 中，可在单个 Linux 应用容器内运行。

## 目录

- src/collection_logistics/：保护站、巡护路线、应急资源、调拨计划和治理情景；
- src/taxonomy_lab/：调查协议、观察记录、异常复核、分析任务租约和生态结论；
- src/biosafety_ops/：园区监测、风险告警、处置工单和资源分配；
- src/site_review/：选址评估对象的版本化综合结论、证据批次修订、职责顺序会签、回避与原子发布；
- fixtures/：离线验收使用的调查协议与结构化观察记录（含 fixtures/site_review/ 三域协议与边界数据）；
- tests/：领域规则、事务边界、权限、HTTP API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时只依赖 Python 标准库与 SQLite

## 测试

~~~bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
~~~

## 构建检查

~~~bash
python3 -m compileall -q src tests
~~~

## 离线验收

~~~bash
PYTHONPATH=src python3 -m collection_logistics.acceptance --workspace .
PYTHONPATH=src python3 -m taxonomy_lab.acceptance --workspace .
PYTHONPATH=src python3 -m biosafety_ops.acceptance
PYTHONPATH=src python3 -m site_review.acceptance --workspace .
~~~

前三条命令会在临时 SQLite 数据库中完成保护站和路线登记、资源调拨、生态观察分析与风险处置；选址会签验收会完成三域协议与批次冻结、版本草案创建、顺序会签、证据补充级联阻断、回避补签、原子发布与发布后撤回不可改写历史的完整核对。均不访问外部网络。

## HTTP 服务

~~~bash
PYTHONPATH=src python3 -m collection_logistics.api --database park.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m taxonomy_lab.api --database ecology.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m biosafety_ops.api --database safety.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m site_review.api --database site_review.sqlite3 --host 127.0.0.1 --port 8083
~~~

服务提供浏览器无关的 JSON 接口和健康检查。进程重启后可以继续读取 SQLite 中的业务状态与审计历史。

### 选址会签后台要点（src/site_review/）

- **冻结三要素**：创建结论版本时固化三域调查协议（id@version+SHA-256）、证据批次修订号与清单摘要、GeoJSON 空间边界，整体计算 `freeze_sha256`；冻结内容随版本永久保存。
- **证据只增不改**：证据补充、撤回、失效一律追加批次修订；同一事务内把引用该批次的未发布草案置为 `blocked` 并失效其全部有效签署，已发布/被取代版本不被触动。
- **职责顺序会签**：林业（林下植被）→生态（鸟类繁殖地）→工程（雨季排水），签名对冻结摘要、顺序、前序签名链取摘要；部分唯一索引保证每步一个有效签署，重复签署不产生新记录。
- **回避**：当事人或协调人申报利益关联后，当事人有效签署及其后续签名链立即失效，同版本可由同角色人员补签，无需重建版本。
- **原子发布**：单事务（校验保存点）确认会签完整性与顺序、签名摘要可重算、回避无交集、协议/清单/边界/冻结摘要完整；失败也会留下 `publication.rejected` 审计事件，成功则版本转 `published` 并把旧发布版本置 `superseded`，重复发布返回同一决定号。
- **当前结论与审计**：`GET /subjects/{id}/current` 返回当前可执行决定；`/subjects/{id}/versions`、`/subjects/{id}/audit`、`/evidence_batches/{id}/audit` 供审计人员追溯每次修订、阻断与签署依据。
