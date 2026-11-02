# agentlab

> 把日常的笔记、网页、读书摘录变成可以对话的「第二大脑」。
> 纯 Python 标准库实现，零第三方依赖，没有 API key 也能跑通全流程。

一个用于学习 **AI Agent / RAG** 的完整项目：从模型调用、工具协议、ReAct 循环，
到文档摄取、混合检索，再到 Web 界面和流式对话，每一层都自己实现，没有黑盒。

```
用户提问 ──► Agent（ReAct 循环）
                │
                ├─► 工具：search_knowledge ─► 混合检索（FTS5 关键词 + 向量语义）
                │                                   │
                └─► 工具：calculator 等             └─► SQLite（文档 / 片段 / 向量）
                                                    ▲
                     录入网页或文字 ─► 清洗 ─► 分块 ─► 嵌入 ─┘
```

---

## 它解决什么问题

你收藏了很多文章、记了很多碎片笔记，但真要用的时候找不到。
这个项目让它们变成可以「问」的知识库：

- 录入：粘贴一段文字，或给一个网址（自动抓正文）
- 检索：关键词 + 语义双路召回，用 RRF 融合排序
- 对话：Agent 自己决定「先检索、再回答」，回答基于你库里的内容
- 全部本地：数据在 `data/knowledge.db`，不上传任何地方

---

## 快速开始

```bash
git clone https://github.com/zhou-peng-yuan-0121/learn.git
cd learn

# 1. 不配 key 也能先跑通（用假模型/离线嵌入）
python -m unittest discover -s tests     # 168 个测试
python -m agentlab demo-agent            # 看一次完整 ReAct 轨迹
python -m agentlab demo-llm              # 看流式输出

# 2. 启动 Web 界面（此时为「纯检索」模式）
python -m agentlab serve
# 打开 http://127.0.0.1:7860

# 3. 想让它真的会回答问题：配置模型
cp .env.example .env        # Windows: copy .env.example .env
#   填 AGENTLAB_API_KEY=sk-xxx
python -m agentlab serve    # 重新启动，Agent 上线
```

要求：Python 3.10+。**不需要 pip install 任何东西。**

---

## 命令行

| 命令 | 作用 |
|---|---|
| `agentlab serve` | 启动 Web 界面（录入 / 检索 / 流式对话） |
| `agentlab ingest --text "…" --title 标题` | 录入一段文字 |
| `agentlab ingest --url https://…` | 抓取网页正文并录入 |
| `agentlab docs` | 列出知识库里的文档 |
| `agentlab search "问题" --explain` | 纯检索（不花 token），显示每条结果来自哪一路 |
| `agentlab ask "问题"` | 让 Agent 带检索回答问题（需 API key） |
| `agentlab demo-agent` | 用脚本化假模型演示完整循环（无需 key） |
| `agentlab demo-llm` | 演示流式输出 |
| `agentlab show-tools` | 打印自动生成的工具 JSON Schema |

---

## 架构

六层，每层只依赖它下面那层：

```
Web 层      server.py + static/index.html   路由、JSON、SSE、单页前端
Agent 层    agent.py + tools.py              ReAct 循环、工具注册与执行
检索层      retrieval.py                     FTS5 关键词 + 向量余弦 + RRF 融合
摄取层      ingest.py                       抓取、HTML 清洗、分块、入库
存储层      storage.py                      SQLite：documents / chunks / chunks_fts
模型层      llm.py + embedding.py           兼容客户端、流式解析、假模型
```

```
learn/
├── agentlab/
│   ├── config.py           环境变量 → 不可变 Config
│   ├── llm.py              OpenAI 兼容客户端 + 流式 SSE + FakeLLM
│   ├── tools.py            @tool 装饰器 → 自动生成 JSON Schema
│   ├── builtin_tools.py    安全计算器（AST 白名单）、当前时间
│   ├── agent.py            ReAct 循环 + 三类失败兜底
│   ├── embedding.py        嵌入器协议、离线哈希嵌入、余弦相似度
│   ├── storage.py          SQLite + FTS5 全文索引
│   ├── ingest.py           HTML 清洗、段落优先分块、摄取管线
│   ├── retrieval.py        混合检索、RRF 融合
│   ├── knowledge_tools.py  把检索包装成 Agent 工具
│   ├── server.py           零依赖 HTTP 服务（http.server）
│   ├── static/index.html   单文件 SPA（无构建步骤）
│   └── __main__.py         CLI 入口
└── tests/                  168 个测试，全部脱网可跑
```

---

## 几个关键设计决策

**为什么不用 LangChain / FastAPI / 向量数据库？**
这个项目的目的是**看懂**而不是快速堆功能。零依赖意味着：任何环境 clone 下来就能跑，
每行代码都能追到底。把框架换成自己的实现之后，再用框架时你会知道它在替你做什么。

**为什么工具 schema 要自动生成？**
`@tool` 装饰器用 `inspect.signature` + `typing.get_type_hints` 反射函数签名，
自动产出 JSON Schema。手写 schema 必然出现「代码改了、文档没改」的不一致，
模型就会按过时的菜单点菜。

**为什么工具报错不抛异常，而是喂回模型？**
Agent 循环里，工具失败被包装成「观察」写回对话，模型下一轮通常会自己修正参数。
直接抛异常会让整个任务失败——而「失败信息」本身就是模型最需要的上下文。

**为什么分块要带重叠？**
关键句如果正好压在块的边界上，会被劈成两半，两块都读不懂。
前后重叠 80 字，保证它至少完整地出现在某个块里。

**为什么相似度下限由嵌入器自己声明？**
实测哈希嵌入对完全无关的文本也会给出 ~0.19 的余弦（哈希碰撞噪声），
而真实匹配通常 > 0.4。阈值是**嵌入器的属性**，不该由检索层拍脑袋定：
`HashingEmbedder.suggested_min_similarity = 0.3`，`OpenAIEmbedder` 是 `0.25`。

**为什么用 RRF 而不是加权分数相加？**
bm25 是负数、余弦是 -1~1，两路分数尺度不可比，加权相加要反复调参。
RRF 只看排名：`score = Σ 1/(60 + rank)`，零调参、对尺度免疫。

**为什么关键词检索要两条路径？**
SQLite 的 trigram 分词器要求查询 ≥ 3 字符，中文「向量」这种 2 字词会 0 命中；
中文长句没有空格，会被当成一个短语找精确子串。所以：FTS 负责长词精确命中，
LIKE + **2 字滑窗**（向量/量检/检索/索怎/怎么/工…）负责兜底召回。

---

## 测试

```bash
python -m unittest discover -s tests          # 全部
python -m unittest tests.test_agent -v        # 单模块
```

| 测试文件 | 覆盖 |
|---|---|
| `test_llm.py` | 响应解析、重试策略、SSE 流式、假模型 |
| `test_tools.py` | schema 生成、执行防御、AST 计算器安全 |
| `test_agent.py` | 循环、JSON 容错、工具自愈、步数上限 |
| `test_embedding.py` | 余弦数学性质、离线嵌入确定性 |
| `test_storage.py` | 事务、级联删除、FTS 索引 |
| `test_ingest.py` | 分块边界、HTML 清洗、端到端入库 |
| `test_retrieval.py` | 双路召回、RRF 融合、中文长句 |
| `test_server.py` | 真 HTTP 服务、SSE 事件流、降级路径 |

测试全部使用 `FakeLLM` / `HashingEmbedder`，**不联网、不花钱、0.2 秒内跑完**。

---

## 已知限制

- 向量检索是暴力遍历（O(N×d)），一两万条以内够用，再大需要引入 HNSW 之类的索引
- 离线嵌入器只懂字面相似，同义词需要配置真实的 embedding 模型
- 网页抓取不支持 JS 渲染的页面（拿不到正文时会明确报错）
- 单用户、无鉴权，仅适合本机使用
- 对话历史不落库，重启即丢

## 路线图

- [ ] 对话历史持久化 + 多轮上下文
- [ ] 文档删除/编辑的 Web 操作
- [ ] 检索质量评测集（召回率 / MRR）
- [ ] 对话轨迹回放页面
- [ ] 可选的真实嵌入模型一键切换

## License

MIT
