# 跨海光缆故障与抢修协调

纯Python标准库实现的跨海光缆故障与抢修协调原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、海况、船机许可、备缆窗口和接续质量、冲突检查以及航行通告规则。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、失败场景和航行通告测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8330
```

默认端口为`8330`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 航行通告（渔政作业确认）

抢修船动员（`mobilized`）后，渔政同意不再只靠电话确认，须在系统内登记航行通告并取得回执：

- 调度员（`dispatcher`）登记：海域 `sea_area`、作业起止时间 `start_at`/`end_at`（ISO8601，无时区按UTC）、联系单号 `contact_no`。
- **同一海域时段重叠的通告（待确认或已同意）只能有一份**，由数据库事务强制；退回、撤销、顺延失效后占用释放。
- 渔政（`fisheries_officer`）可同意（登记回执号 `receipt_no`）或退回（必须填写 `reject_reason`）；退回保留拒批原因，需用**新联系单号重新登记**。
- **渔政同意前勘察（`survey`）不能开始**，接口返回409并说明阻塞原因。
- 作业期顺延由调度员办理：旧单标记为 `superseded`（原回执立即失效），换发待确认新单，须按新时段再次经渔政同意。
- 撤销通告后海域占用释放。记录列表与详情均返回 `navigational_notice` 字段，含 `valid_receipt`（有效回执）与 `block_reason`（阻塞原因）。

通告接口：

- `POST /api/records/{id}/notices`：登记通告，请求体 `{"data":{"sea_area":"...","contact_no":"...","start_at":"...","end_at":"..."}}`。
- `GET /api/records/{id}/notices`：该抢修单的通告历史（含退回原因、回执、撤销原因）。
- `POST /api/notices/{id}/actions/approve_notice`：渔政同意，数据含 `expected_version`、`receipt_no`。
- `POST /api/notices/{id}/actions/reject_notice`：渔政退回，数据含 `expected_version`、`reject_reason`。
- `POST /api/notices/{id}/actions/revoke_notice`：撤销（调度员或渔政），数据含 `expected_version`、`revoke_reason`。
- `POST /api/notices/{id}/actions/reschedule_notice`：作业期顺延（调度员），数据含 `expected_version` 和新的 `end_at`（可同时给出 `start_at`、`sea_area`、`contact_no`）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
