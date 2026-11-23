# 贡献指南

1. 阅读 `docs/dev-guidelines.md` 与对应周任务书；不要提前实现后续周次的安全/写操作。
2. 新增依赖必须进入 `pyproject.toml` 与 `requirements.lock.txt`；训练依赖（torch/transformers/peft 等）不得进入 Alpha 依赖。
3. 每个功能必须附带单元测试；涉及数据库的改动需要集成测试或明确的 SKIPPED 说明。
4. 提交信息使用 `feat(Wxx): ...` / `fix(Wxx): ...` / `docs(Wxx): ...`。
5. 不要提交 `.env`、真实密码、Token、私钥或生产连接串；示例文件只使用 `CHANGE_ME` 占位。
6. 不修改或删除 `项目规划/` 下的原始文档。
7. 每周交付需由独立验收方对照任务书逐条核对，实现者不得自签。
