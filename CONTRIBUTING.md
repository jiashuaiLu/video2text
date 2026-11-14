# 参与贡献

感谢你有兴趣。这个项目优先保证「**开箱能用**」，所以提 PR 前请先看这几条。

## 提 Issue

- **Bug**：请附上链接类型（抖音 / B站 / 本地文件）、系统版本、芯片（Intel 还是 Apple Silicon）、以及 `out/` 里对应的日志片段
- **功能建议**：先说清楚「你在什么场景下、卡在哪一步」，比直接说「加个 XX 功能」有用得多
- **转写质量问题**：请给出原视频链接 + 出错的时间点 + 你认为正确的写法，这类问题很多是模型能力边界，不一定是 bug

## 提 PR

1. 先开 issue 讨论，避免白做
2. 一个 PR 只做一件事
3. 提交前本地跑一遍：

```bash
.venv/bin/python -m compileall -q bin
gh workflow run ci.yml   # 或直接等 CI
```

## 代码约定

| 位置 | 说明 |
|---|---|
| `bin/v2t.py` | 主流程，改动要与现有风格一致（函数短、日志清晰） |
| `bin/formatmd.py` | 排版规则。**改规则请补测试用例**，否则很容易把别的地方改坏 |
| `bin/glossary.py` | 拼音纠错。改索引结构时务必跑 `tests/test_glossary.py` |
| `bin/llm.py` | 模型后端。不要在这里打印或记录 API Key |

## 不要做的事

- 不要引入需要联网才能启动的依赖
- 不要默认调用云端接口（本地优先是这个项目的底线）
- 不要在日志、异常信息里输出完整 API Key
- 不要提交 `config.json`、`work/`、`out/`、模型文件

## 开发环境

```bash
./setup.sh                  # 装依赖
./bin/v2t "<链接>" --no-format   # 最快验证转写链路
cd desktop && npx tauri dev     # 调界面
```
