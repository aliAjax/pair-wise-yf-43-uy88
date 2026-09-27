# 实验室仪器校准与方法验证

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8309`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8309
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `instrument`：仪器状态；`calibration`：校准记录（含逐点登记的校准点）；`method`：方法版本；`result`：检测结果。

## 校准点登记

- 校准记录`perform`通过（`passed`/`approved`）后，用`register_point`动作逐点登记校准点：`{"action":"register_point","data":{"point":{"standard":0,"indicated":0.02,"uncertainty":0.01,"due_at":"2027-09-30"}}}`，每点记录标准值、示值、扩展不确定度和到期日，登记不改变校准记录状态。
- 重复送检会创建新的校准记录，旧记录及其校准点全部保留；未过期的旧点继续参与覆盖。

## 放行修正

- 放行结果（`release`）时，系统收集该仪器所有已通过校准记录中未到期的校准点，仅当测得值落在两个已校准点之间才允许通过。
- 修正量按相邻两点的（标准值−示值）线性插值计算，响应数据包含`original_value`（原始值）、`correction`（修正量）、`corrected_value`（修正后数值）和`calibration_points`（采用的校准点，含来源校准记录）。
- 校准点不足、测得值超出覆盖区间或校准过期时，结果保持`pending`状态，`hold_reason`字段说明原因；补足校准点后可重新放行。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

校准周期、误差和放行规则是可演示的业务模型，不替代实验室质量体系或计量认证。
