# 燃气管线泄漏检测与隔离协调

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8333`。

- `app.py`：参数、依赖和服务生命周期。
- `src/domain.py`：管段、传感值和来源记录校验。
- `src/rules.py`：泄漏评分、阀门顺序、修复、试压、恢复状态机。
- `src/repository.py`：SQLite、重复保护、乐观版本和审计链。
- `src/service.py`：角色权限和业务编排。
- `src/http_api.py`：JSON 接口与首页。
- `src/audit.py`：可校验的审计事件。

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8333
python3 -m unittest discover -s tests -v
```

接口包括 `GET /health`、`GET /api/state`、`GET /api/valves`、`POST /api/items`、`POST /api/items/<id>/sources`、`POST /api/items/<id>/actions` 和审计查询。

阀门开关集中为共享台账（`valve_ledger` 表）：隔离时把阀号挂到在处置事件名下，已被其他在处置事件占用的阀门不重复登记，响应的 `operation.ledger.skipped` 说明归属；恢复时只放开没有其他在处置事件占用的阀门，仍被占用的在 `operation.ledger.held` 中列明并保持关闭。台账登记/释放与事件状态变更在同一事务提交，`GET /api/valves` 可查每台阀的归属、开关状态和占用方。

所有操作都要带 `expected_version`，同一事件的并发提交由事务内版本检查拒绝晚到者（409）。`isolate`/`restore` 等阀门操作校验辖区：事件创建时记录 `region`（请求体或 `X-Region` 头），操作者辖区不匹配返回 403（`regulator` 除外）。

测试覆盖完整抢修流程、重复事件、阀门顺序、试压阈值、现场危险条件、权限和版本冲突。模型不替代 SCADA、管网水力计算或正式应急预案。
