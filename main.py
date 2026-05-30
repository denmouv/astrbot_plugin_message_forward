import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime
from typing import Dict

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain, Reply
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.platform.message_session import MessageSession

# 消息标签正则: [ref:xxxxxxxx]
_TAG_PATTERN = re.compile(r"`ref:([a-zA-Z0-9]+)`")

# send_by_session 未实现的平台（需要绕过直接使用客户端 API）
_SEND_BY_SESSION_NOOP_PLATFORMS = {"weixin_official_account"}


class MessageForwardPlugin(Star):
    """消息转发助手插件（v2）

    管理员通过长按消息→引用回复，回复内容自动转发给用户。

    无状态设计，通过消息标签 [ref:xxx] 关联管理员与其回复的用户。
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # 数据目录
        self._data_dir = StarTools.get_data_dir("astrbot_plugin_message_forward")
        self._tags_file = self._data_dir / "message_tags.json"

        # 消息标签 → 用户会话映射
        # key: "ref:xxxxxxxx" → {"user_umo": "...", "user_name": "...", "created_at": timestamp}
        self._message_tags: Dict[str, dict] = {}

        # 转发历史（仅记录，不用于路由）
        self._forward_history: list[dict] = []

        # 手动模式会话: user_umo → {"admin_umo": "...", "expires_at": timestamp}
        self._manual_sessions: Dict[str, dict] = {}

        # 文件读写锁
        self._file_lock = asyncio.Lock()

        # 后台清理任务
        self._cleanup_task: asyncio.Task | None = None

    # ==================== 生命周期 ====================

    async def initialize(self):
        os.makedirs(self._data_dir, exist_ok=True)
        await self._load_tags()
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info(f"消息转发插件 v2 已初始化，已加载 {len(self._message_tags)} 个消息标签")

    async def terminate(self):
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        logger.info("消息转发插件 v2 已停用")

    # ==================== 消息拦截钩子（管理员引用回复 + 用户手动模式）====================

    @filter.event_message_type(filter.EventMessageType.ALL, priority=sys.maxsize)
    async def on_message_intercept(self, event: AstrMessageEvent):
        """统一消息拦截：管理员引用回复转发 + 手动模式用户消息转发。"""
        # 跳过 Bot 自己发出的消息
        if event.get_sender_id() == event.get_self_id():
            return

        user_umo = event.unified_msg_origin
        admin_sessions = self._get_admin_sessions()

        # ---- 分支 1: 手动模式用户消息 → 转发给管理员 ----
        manual = self._manual_sessions.get(user_umo)
        if manual is not None:
            now = time.time()
            if now >= manual["expires_at"]:
                del self._manual_sessions[user_umo]
                logger.info(f"手动模式已超时退出: {user_umo}")
                return

            user_msg = event.get_message_str()
            if not user_msg.strip():
                return

            # 生成新标签，管理员可引用此消息回复
            tag = await self._create_tag(user_umo, event.get_sender_name())

            admin_umo = manual["admin_umo"]
            forward_text = (
                f"💬 用户消息\n"
                f"---\n"
                f"{user_msg}\n"
                f"---\n"
                f"💡 引用此消息回复 `{tag}`"
            )
            chain = MessageChain().message(forward_text)
            try:
                await self.context.send_message(admin_umo, chain)
                logger.info(f"手动模式转发: {user_umo} → {admin_umo}")
            except Exception as e:
                logger.error(f"手动模式转发失败: {e}")

            # 注入到用户对话历史（记录用户说了什么）
            await self._inject_to_conversation(user_umo, "user", user_msg)

            manual["expires_at"] = now + 3600
            event.stop_event()
            return

        # ---- 分支 2: 非管理员会话 → 不处理 ----
        if not admin_sessions or user_umo not in admin_sessions:
            return

        # ---- 分支 3: 管理员引用回复 → 转发给用户 ----
        messages = event.get_messages()
        reply_comp = None
        for comp in messages:
            if isinstance(comp, Reply):
                reply_comp = comp
                break

        if reply_comp is None:
            # 不是引用回复，让管理员 Bot 的 LLM 正常处理
            return

        # 从被引用的消息中提取标签
        replied_text = reply_comp.message_str or ""
        tag_match = _TAG_PATTERN.search(replied_text)
        if not tag_match:
            logger.info(f"引用回复中未找到消息标签，不触发转发。原文: {replied_text[:100]}")
            return

        tag = f"ref:{tag_match.group(1)}"
        tag_data = self._message_tags.get(tag)
        if not tag_data:
            logger.info(f"消息标签 {tag} 已过期或不存在")
            yield event.plain_result(f"⚠️ 该消息标签（{tag}）已过期或不存在，无法自动转发。请使用其他方式回复。")
            event.stop_event()
            return

        # 提取管理员回复内容（排除 Reply 组件）
        reply_text_parts = []
        for comp in messages:
            if isinstance(comp, Plain):
                text = comp.text
                if text:
                    reply_text_parts.append(text)

        reply_text = "".join(reply_text_parts).strip()
        if not reply_text:
            yield event.plain_result("⚠️ 回复内容为空，未转发。")
            event.stop_event()
            return

        # 发送给用户
        prefix = self.config.get("reply_prefix", "[管理员回复]\n")
        target_umo = tag_data["user_umo"]
        user_name = tag_data.get("user_name", "用户")
        forward_text = f"{prefix}{reply_text}"

        chain = MessageChain().message(forward_text)
        ok = await self._send_message(target_umo, chain)
        if not ok:
            logger.error(f"引用回复转发失败: {tag} → {target_umo}")
            yield event.plain_result(f"❌ 转发失败，请稍后重试。")
            event.stop_event()
            return
        logger.info(f"引用回复已转发: {tag} → {target_umo}")

        # 注入对话历史
        await self._inject_to_conversation(target_umo, "assistant", forward_text)
        await self._inject_to_conversation(user_umo, "user", reply_text)
        confirmation = self.config.get("reply_confirmation", "")
        if confirmation:
            confirm_msg = confirmation.format(user_name=user_name)
            await self._inject_to_conversation(user_umo, "assistant", confirm_msg)

        # 记录历史
        self._forward_history.append({
            "tag": tag,
            "direction": "admin->user",
            "user_umo": target_umo,
            "user_name": user_name,
            "admin_umo": user_umo,
            "admin_name": event.get_sender_name(),
            "content": reply_text,
            "timestamp": datetime.now().isoformat(),
        })
        await self._trim_history()

        if confirmation:
            confirm_msg = confirmation.format(user_name=user_name)
            yield event.plain_result(confirm_msg)

        event.stop_event()

    # ==================== LLM 工具: forward_to_admin ====================

    @filter.llm_tool(name="forward_to_admin")
    async def forward_to_admin(self, event: AstrMessageEvent, reason: str, summary: str):
        '''当 bot 无法回答用户的问题时，将问题转发给管理员处理。
        仅当用户明确要求人工客服、或 bot 确实无法回答问题时才调用此工具。

        Args:
            reason(string): 转发原因，例如"无法回答技术问题"、"用户要求人工客服"
            summary(string): 用户问题的简明摘要，概述用户的需求
        '''
        admin_sessions = self._get_admin_sessions()
        if not admin_sessions:
            logger.warning("forward_to_admin: 未配置管理员会话")
            return "未配置管理员会话，无法转发。请联系管理员在插件配置中设置 admin_sessions。"

        # 生成消息标签
        original_umo = event.unified_msg_origin
        original_msg = event.get_message_str()
        sender_name = event.get_sender_name()
        tag = await self._create_tag(original_umo, sender_name)

        # 构建转发消息（用 summary 作为主体，原始消息引用在下方）
        msg_body = summary if summary else original_msg[:200]
        forward_msg = (
            f"📨 转人工 · `{tag}`\n"
            f"---\n"
            f"{msg_body}\n"
            f"---\n"
            f"> 原消息: {original_msg[:200]}\n"
            f"💡 引用此消息回复 `{tag}`"
        )

        # 发送给所有管理员会话
        chain = MessageChain().message(forward_msg)
        success_count = 0
        for session_str in admin_sessions:
            try:
                await self.context.send_message(session_str, chain)
                success_count += 1
            except Exception as e:
                logger.error(f"转发到管理员会话 {session_str} 失败: {e}")

        # 通知用户
        if self.config.get("enable_notification", True):
            notify_template = self.config.get(
                "notification_message",
                "您的问题已转接给人工客服处理，请耐心等待回复。（会话编号: {ref_tag}）",
            )
            notify_text = notify_template.format(ref_tag=tag)
            try:
                notify_chain = MessageChain().message(notify_text)
                await self._send_message(original_umo, notify_chain)
            except Exception as e:
                logger.error(f"发送用户通知失败: {e}")

        # 记录历史
        self._forward_history.append({
            "tag": tag,
            "direction": "user->admin",
            "user_umo": original_umo,
            "user_name": sender_name,
            "admin_umo": admin_sessions[0],
            "reason": reason,
            "summary": summary,
            "original_message": original_msg,
            "timestamp": datetime.now().isoformat(),
        })
        await self._trim_history()

        if success_count > 0:
            # 标记用户进入手动模式，后续消息直接转发管理员
            self._manual_sessions[original_umo] = {
                "admin_umo": admin_sessions[0],
                "expires_at": time.time() + 48 * 3600,
            }
            return (
                f"消息已成功转发给管理员（编号: {tag}），已通知 {success_count} 个管理员会话。"
                f"管理员可通过引用回复此消息来回复用户。"
            )
        else:
            return "转发失败：无法发送消息给任何管理员会话，请检查管理员会话配置。"

    # ==================== 消息标签管理 ====================

    def _generate_tag(self) -> str:
        """生成唯一消息标签: ref:xxxxxxxx"""
        return f"ref:{os.urandom(4).hex()}{int(time.time() * 1000) % 10000:04d}"

    async def _create_tag(self, user_umo: str, user_name: str) -> str:
        """生成标签并存储映射。返回标签字符串。"""
        tag = self._generate_tag()
        self._message_tags[tag] = {
            "user_umo": user_umo,
            "user_name": user_name,
            "created_at": time.time(),
        }
        await self._save_tags()
        return tag

    async def _send_message(self, session_str: str, message_chain: MessageChain) -> bool:
        """发送消息到指定会话，自动处理平台差异。

        部分平台（如微信公众号）的 send_by_session 是空实现，
        需要绕过它直接使用平台客户端 API 发送。
        """
        try:
            session = MessageSession.from_str(session_str)
        except Exception as e:
            logger.error(f"_send_message: 解析会话失败: {e}")
            return False

        platform_id = session.platform_id
        if platform_id in _SEND_BY_SESSION_NOOP_PLATFORMS:
            # 绕过空实现，直接使用平台客户端
            platform = self.context.get_platform_inst(platform_id)
            if not platform:
                logger.error(f"_send_message: 未找到平台 {platform_id}")
                return False
            client = platform.get_client() if hasattr(platform, "get_client") else None
            if not client:
                logger.error(f"_send_message: 平台 {platform_id} 无可用客户端")
                return False

            target_user = session.session_id
            for comp in message_chain.chain:
                if isinstance(comp, Plain):
                    try:
                        client.message.send_text(target_user, comp.text)
                        logger.info(f"_send_message: 通过客户端发送到 {platform_id}:{target_user}")
                    except Exception as e:
                        logger.error(f"_send_message: 客户端发送文本失败: {e}")
                        return False
                else:
                    logger.warning(f"_send_message: 平台 {platform_id} 不支持组件类型 {type(comp).__name__}，已跳过")
            return True

        # 标准路径：使用 context.send_message
        try:
            return await self.context.send_message(session, message_chain)
        except Exception as e:
            logger.error(f"_send_message: context.send_message 失败: {e}")
            return False

    def _get_admin_sessions(self) -> list[str]:
        """从配置中解析管理员会话列表（每行一个 unified_msg_origin）。"""
        text = self.config.get("admin_sessions", "")
        if not text or not text.strip():
            return []
        return [line.strip() for line in text.strip().split("\n") if line.strip()]

    async def _inject_to_conversation(self, umo: str, role: str, content: str) -> bool:
        """将一条消息注入到指定会话的 LLM 对话历史中。

        解决管理员通过插件回复用户时，回复内容只走了平台推送、
        没有写入 ConversationManager 导致 LLM 上下文缺失的问题。
        """
        try:
            conv_mgr = self.context.conversation_manager
            cid = await conv_mgr.get_curr_conversation_id(umo)
            if not cid:
                logger.debug(f"_inject_to_conversation: {umo} 没有活跃对话，跳过")
                return False
            conv = await conv_mgr.get_conversation(umo, cid)
            if not conv:
                return False
            history = json.loads(conv.history) if conv.history else []
            history.append({"role": role, "content": content})
            await conv_mgr.update_conversation(umo, cid, history=history)
            logger.info(f"已注入消息到 {umo} 对话历史: [{role}] {content[:60]}...")
            return True
        except Exception as e:
            logger.error(f"注入对话历史失败 ({umo}): {e}")
            return False

    # ==================== 持久化 ====================

    async def _load_tags(self):
        """从 JSON 文件加载消息标签和历史。"""
        async with self._file_lock:
            try:
                if os.path.exists(self._tags_file):
                    with open(self._tags_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    self._message_tags = data.get("tags", {})
                    self._forward_history = data.get("history", [])
                    logger.info(
                        f"已加载 {len(self._message_tags)} 个标签，"
                        f"{len(self._forward_history)} 条历史记录"
                    )
            except Exception as e:
                logger.error(f"加载数据失败: {e}")
                self._message_tags = {}
                self._forward_history = []

    async def _save_tags(self):
        """将消息标签和历史持久化到 JSON 文件。"""
        async with self._file_lock:
            try:
                data = {
                    "tags": self._message_tags,
                    "history": self._forward_history,
                }
                with open(self._tags_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.error(f"保存数据失败: {e}")

    async def _trim_history(self):
        """裁剪历史记录，保留最近 max_history 条。"""
        max_history = self.config.get("max_history", 500)
        if len(self._forward_history) > max_history:
            self._forward_history = self._forward_history[-max_history:]
            await self._save_tags()

    # ==================== 过期标签清理 ====================

    async def _cleanup_loop(self):
        """后台定期清理过期标签。"""
        while True:
            try:
                await asyncio.sleep(3600)  # 每小时检查一次
                expire_hours = self.config.get("tag_expire_hours", 72)
                if expire_hours <= 0:
                    continue

                now = time.time()
                expired = []
                for tag, data in self._message_tags.items():
                    age_hours = (now - data.get("created_at", 0)) / 3600
                    if age_hours > expire_hours:
                        expired.append(tag)

                if expired:
                    for tag in expired:
                        del self._message_tags[tag]
                    await self._save_tags()
                    logger.info(f"已清理 {len(expired)} 个过期标签")

                # 清理过期的手动模式会话
                expired_manual = [
                    umo for umo, data in self._manual_sessions.items()
                    if now >= data["expires_at"]
                ]
                for umo in expired_manual:
                    del self._manual_sessions[umo]
                if expired_manual:
                    logger.info(f"已清理 {len(expired_manual)} 个过期手动模式会话")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"标签清理出错: {e}")
                await asyncio.sleep(60)
