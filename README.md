# Explainable Rule Adjudication API

一个可解释的规则裁决服务：客户端提交由 `all` / `any` / `not` / 比较运算组成的策略（每条规则含**优先级**与**结论**），随后对事实（facts）求值。求值采用**真 / 假 / 未知**三值逻辑，按优先级层级裁决，并返回逐节点推导轨迹；当裁决层无法判定时，给出所有**包含关系最小的缺失字段集合**。

技术栈：Python 3.12 · FastAPI · 内存存储（重启即清空，无外部依赖）。

---

## 一、启动（单条命令）

```bash
docker compose up --build
```

依赖（FastAPI / Uvicorn / Pydantic）在镜像构建时随启动流程自动安装，无需任何本地环境。

## 二、配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `PORT` | `8000` | 容器内服务监听端口（Dockerfile / Compose 均读取） |

宿主机端口映射见 `docker-compose.yml` 的 `ports`（默认 `8000:8000`，可改为 `"8080:8000"` 等）。

## 三、API 地址

| 地址 | 说明 |
|---|---|
| `http://localhost:8000` | API 根地址 |
| `http://localhost:8000/docs` | Swagger 交互式文档 |
| `http://localhost:8000/openapi.json` | OpenAPI 规范 |
| `http://localhost:8000/health` | 健康检查 |

本地开发（不用 Docker）：

```bash
pip install -r requirements.txt -r requirements-dev.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
pytest            # 运行测试
```

---

## 四、策略 DSL

策略是一个 JSON 对象：

```json
{
  "policy_id": "loan-eligibility",
  "description": "可选说明",
  "rules": [
    {
      "rule_id": "adult-with-income",
      "priority": 10,
      "conclusion": "eligible",
      "condition": {
        "all": [
          {"cmp": {"op": ">=", "left": {"field": "age"}, "right": 18}},
          {"cmp": {"op": ">=", "left": {"field": "income"}, "right": 30000}}
        ]
      }
    }
  ]
}
```

**条件表达式**（递归结构，每个节点恰好一个键）：

| 节点 | 形式 | 语义 |
|---|---|---|
| 合取 | `{"all": [cond, ...]}` | 全部子条件为真才为真（非空数组） |
| 析取 | `{"any": [cond, ...]}` | 任一子条件为真即为真（非空数组） |
| 否定 | `{"not": cond}` | 真↔假，未知仍为未知 |
| 比较 | `{"cmp": {"op": ..., "left": ..., "right": ...}}` | 见下 |

**比较运算符**：`==`、`!=`、`<`、`<=`、`>`、`>=`、`in`（左操作数是否属于右操作数集合）。

**操作数**：

- `{"field": "a.b"}` —— 事实字段引用，支持点号嵌套路径；
- `{"const": <任意 JSON>}` —— 常量（数组常量必须用这种形式）；
- 裸标量（字符串 / 数字 / 布尔 / null）—— 等同常量。

**规则字段**：`rule_id`（策略内唯一）、`priority`（数值，**越大越优先**）、`conclusion`（任意 JSON 值）、`condition`（条件表达式）、`description`（可选）。

## 五、裁决语义

1. 规则按 `priority` 分组为层级，**从高优先级到低优先级**依次检查；
2. 全部规则为**假**的层级被跳过；第一个含有**真**或**未知**规则的层级成为**裁决层**；
3. 裁决层中：
   - 存在**未知**规则 → 返回 `unknown`，附逐节点推导轨迹与所有包含关系最小的缺失字段集合；
   - 全部已知且为真规则的结论**唯一** → 返回 `concluded` 与该结论；
   - 全部已知但为真规则的结论**不一致** → 返回 `conflict` 与冲突结论列表；
4. 所有层级均为假 → 返回 `no_match`。

**三值逻辑**（Kleene 逻辑）：比较运算引用的字段缺失时为`未知`；`not 未知 = 未知`；`all` 遇假则假、否则遇未知则未知；`any` 遇真则真、否则遇未知则未知。求值关于事实单调：补充字段只会把`未知`变为确定值，不会翻转已确定的值。

**缺失字段集合**：`missing_field_sets` 中每个集合 S 满足——存在对 S 中字段的某种取值，能使裁决层全部规则可判定（真或假），且 S 的任意真子集都不满足该性质（包含关系最小）。集合之间是"或"的关系：任选其一来补充事实即**可能**解开裁决。结果按字典序排列；极端策略下集合数量超过上限时会截断并以 `missing_sets_truncated: true` 标记。

## 六、接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/health` | 健康检查（状态、版本、运行时长、已加载策略数） |
| `POST` | `/policies/validate` | 仅校验策略结构，返回 `{valid, errors}` |
| `POST` | `/policies` | 创建策略（201；结构非法 422；ID 已存在 409） |
| `GET` | `/policies` | 列出全部策略摘要 |
| `GET` | `/policies/{id}` | 获取策略（404） |
| `PUT` | `/policies/{id}` | 整体替换策略（body 的 `policy_id` 须与路径一致） |
| `DELETE` | `/policies/{id}` | 删除策略（204 / 404） |
| `POST` | `/policies/{id}/evaluate` | 对事实求值，body 为 `{"facts": {...}}` |

## 七、示例

```bash
# 1. 健康检查
curl http://localhost:8000/health

# 2. 提交策略
curl -X POST http://localhost:8000/policies -H 'Content-Type: application/json' -d '{
  "policy_id": "loan",
  "rules": [
    {"rule_id": "adult-with-income", "priority": 10, "conclusion": "eligible",
     "condition": {"all": [
       {"cmp": {"op": ">=", "left": {"field": "age"}, "right": 18}},
       {"cmp": {"op": ">=", "left": {"field": "income"}, "right": 30000}}]}},
    {"rule_id": "fallback", "priority": 1, "conclusion": "review",
     "condition": {"cmp": {"op": "==", "left": {"field": "country"}, "right": "US"}}}
  ]
}'

# 3. 事实齐全 → 唯一结论
curl -X POST http://localhost:8000/policies/loan/evaluate \
  -H 'Content-Type: application/json' \
  -d '{"facts": {"age": 30, "income": 50000}}'
# → {"verdict": "concluded", "conclusion": "eligible", "deciding_priority": 10, ...}

# 4. 缺少字段 → 未知 + 推导轨迹 + 最小缺失字段集合
curl -X POST http://localhost:8000/policies/loan/evaluate \
  -H 'Content-Type: application/json' \
  -d '{"facts": {"age": 30}}'
# → {"verdict": "unknown", "missing_field_sets": [["income"]],
#    "trace": {"levels": [{"priority": 10, "status": "deciding", "rules": [
#      {"rule_id": "adult-with-income", "value": "unknown", "trace": {
#        "node": "all", "value": "unknown", "children": [
#          {"node": "cmp", "op": ">=", "value": "true",  ...},
#          {"node": "cmp", "op": ">=", "value": "unknown",
#           "reason": "missing field(s): income", ...}]}}]}]}}, ...}
```

`trace.levels[].rules[].trace` 是逐节点的推导轨迹：每个节点标注 `true` / `false` / `unknown`，比较节点附带左右操作数的解析值或缺失原因，可直接用于前端解释展示。

## 八、项目结构

```
app/
  main.py        # FastAPI 路由（策略 CRUD、校验、求值、健康检查）
  logic.py       # 三值求值、推导轨迹、最小缺失字段集合、层级裁决
  validation.py  # 策略与条件表达式结构校验
  models.py      # 请求模型
  store.py       # 线程安全的内存策略存储
tests/test_api.py
Dockerfile  docker-compose.yml  requirements.txt  requirements-dev.txt
```
