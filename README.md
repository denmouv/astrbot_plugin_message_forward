# astrbot_plugin_message_forward

AstrBot 消息转发助手插件 — 将 bot 无法回答的问题转发给管理员，支持两种回复方式。

## 功能

1. **引用回复**（无感模式）— 管理员收到转发消息后，长按消息→引用回复，内容自动转发给原用户
2. **Agent 工具** — 管理员侧 LLM 调用 `forward_to_session` 工具，将消息发送到指定会话

## 工作原理

```
用户(Bot A) → forward_to_admin(LLM工具) → 管理员(Bot B) → 引用回复 → 自动转发给用户
                                                       → forward_to_session(Agent工具) → 指定会话
```

采用**无状态**设计，通过消息标签 `[ref:xxx]` 关联管理员与其回复的用户，不维护持久桥接状态。

## 安装

1. 在 AstrBot 管理面板中打开插件市场，搜索 `astrbot_plugin_message_forward`
2. 安装并启用
3. 配置 `admin_sessions`（管理员会话的 unified_msg_origin）

## 配置

- `admin_sessions`: 管理员会话列表（每行一个 `platform_id:message_type:session_id`）
- `enable_notification`: 是否通知用户已转接
- `forward_header`: 转发消息头部模板
- `reply_prefix`: 管理员回复前缀
- `tag_expire_hours`: 消息标签过期时间

## 支持平台

- 微信公众号 (weixin_official_account)
- 个人微信 (weixin_oc)
- 及所有 AstrBot 支持的平台

## 开发

```bash
git clone https://github.com/denmouv/astrbot_plugin_message_forward.git
# 将文件夹放入 AstrBot 的 data/plugins/ 目录
```

## 许可

MIT
