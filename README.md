# 跨海光缆故障与抢修协调

纯Python标准库实现的跨海光缆故障与抢修协调原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、海况、船机许可、备缆窗口、接续质量、冲突检查和航行通告规则。
- `src/repository.py`：SQLite建表（records/audit_events/notices）、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8330
```

默认端口为`8330`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数，每条记录附带`notice`（有效回执与阻塞原因）。
- `GET /api/records/{id}`：记录详情，附带`notice`。
- `GET /api/records/{id}/audit`：审计时间线（含通告事件）。
- `GET /api/records/{id}/notices`：该记录的航行通告历史（退回原因保留可查）。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。
- `POST /api/records/{id}/notices`：动员后登记航行通告，请求体为`{"data":{"area":"...","start_time":"...","end_time":"...","contact_no":"..."}}`。
- `POST /api/records/{id}/notices/{notice_id}/actions/{action}`：通告操作，`action`为`confirm`/`reject`/`resubmit`/`extend`/`revoke`，请求体为`{"data":{...}}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 航行通告流程

抢修船进场前不再依赖电话确认，渔业部门意见通过航行通告回执落地：

1. **登记**：记录进入`mobilized`后，调度员（`dispatcher`）登记海域、起止时间和联系单号。同一海域时段重叠的占用中通告（`pending`/`approved`）只能有一份，冲突返回409。
2. **确认**：渔政（`fishery_admin`）`confirm`后生成回执号（如`RCPT-000002-1`）；**渔政同意前勘察（`survey`）不能开始**。
3. **退回**：渔政`reject`需填写拒批原因，原因随通告保留；调度员`resubmit`换新联系单号重提，旧通告留痕。
4. **顺延**：调度员`extend`修改作业时段后原回执失效（号码留存于`voided_receipt_no`），通告回到`pending`，须按新时段重新确认。
5. **撤销**：调度员`revoke`后占用释放，同海域时段可再登记；抢修恢复或取消时占用中的通告自动撤销释放。

列表与详情中的`notice.blocking_reason`给出勘察被阻塞的原因，`notice.receipt_no`为当前有效回执（未确认或已顺延则为空）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
