# 贡献指南 (CONTRIBUTING)

感谢关注 **SIP-SWITCH**！这是一个基于 FreeSWITCH + Python 网关的企业级 SIP 软交换平台（多租户计费、成本计费、预付费、运营商扣费、落地网关自动下发）。在提交代码前，请花几分钟读一下这份简短指南。

## 适用范围

- 当前项目由核心维护者主导，欢迎 Issue 与 PR。
- **暂不接受**以下类型的 PR，如有想法请先开 Issue 讨论：
  - 大规模架构重构；
  - 把路由 / 计费 / 鉴权等业务逻辑硬编码进 FreeSWITCH 本地 dialplan / acl / 网关 XML。

## 架构边界（贡献者必须遵守）

- **网关层（`src/`）**：只做信令路由、计费、业务规则与故障切换决策，不持有 SIP 套接字。
- **FreeSWITCH**：只负责 SIP 信令、媒体流与呼叫桥接执行。落地网关 XML 由网关通过 `fs_provision` 自动下发，请勿手工改 FS 本地配置。

## 开发环境

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

- `python-esl` 不在 PyPI，由 FreeSWITCH 提供（详见 `requirements.txt` 注释与 `README.md` 安装说明）。
- 复制 `config.example.yaml` 为 `config_settings.yaml` 并填入本地配置。该文件已被 `.gitignore` 忽略，**请勿提交**。

## 运行测试

```bash
pytest
```

- 测试位于 `tests/`，使用 `pytest.ini` 的 `pythonpath=src` 配置。
- 部分用例需要本地 `config_settings.yaml`（含 DB / ESL 连接信息），请先配置再跑。

## 代码约定

- 遵循现有代码风格；新增关键路径（路由 / 计费 / 鉴权）请补单元测试。
- 提交信息清晰、聚焦，建议关联对应 Issue。

## 提交流程

1. Fork 或切分支（建议 `feat/xxx`、`fix/xxx`）。
2. 本地自测通过 `pytest`。
3. 发起 PR，描述「为什么改、改了什么、如何验证」。
4. 维护者评审通过后合入 `main`。

## 行为准则

请保持友好、就事论事。骚扰性或攻击性内容将被拒绝。
