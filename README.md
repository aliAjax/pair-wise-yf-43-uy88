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

- `instrument`：仪器状态；`calibration`：校准记录；`method`：方法版本；`result`：检测结果。

## 校准点登记

校准不再只保存一个合格结论。校准执行（`perform`，result=passed）时必须逐点登记 `points`，每个点包含：

- `standard_value`：标准值
- `indicated_value`：仪器示值
- `expanded_uncertainty`：扩展不确定度
- `due_at`：该点到期日（ISO 日期）

校准批准（`approve`）后，这些点追加登记到仪器的 `data.calibration_points`，并记录来源校准记录和登记时间。旧点不会被删除或覆盖，重复送检保留全部历史点（同一示值存在多点时优先使用有效且最新的点）。

## 结果放行

`release` 不再直接使用示值。只有当测得值 `value` 落在两个当前有效（未过期）的已校准示值点之间时才放行，并在结果中返回：

- `raw_value`：原始值
- `correction`：修正量（相邻两点对标准值做线性插值得出）
- `corrected_value`：修正后数值（原始值 + 修正量）
- `expanded_uncertainty`：采用的扩展不确定度（两点取大）
- `calibration_points`：实际采用的两个校准点
- `release_checked_at`：判定使用的日期（默认当天，也可在动作数据中传 `measured_at`）

有效点不足两个、测得值超出覆盖区间、或包围该值的校准点已过期时，结果**保持 `pending`**，不抛异常，并在数据中写入 `reason`（同时在审计时间线中以 `deferred` 记录）。可以继续用 `block`/`reanalyze` 处理，或补齐校准后重新 `release`。

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
