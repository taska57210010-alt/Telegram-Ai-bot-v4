"""
Production-Ready Telegram AI Chat Bot
Architected for 100,000+ concurrent users

Features:
- PostgreSQL with connection pooling
- Redis caching for performance
- Webhook mode (not polling)
- Comprehensive monitoring (Sentry + Prometheus)
- Multi-tier rate limiting
- Atomic payment transactions
- Graceful shutdown
- Health checks
- Admin controls
"""

import asyncio
import logging
import signal
import sys
import time
from typing import Optional

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from config import config
from database import Database
from cache import cache
from rate_limiter import rate_limiter
from monitoring import monitoring, track_time, count_requests, questions_asked_total, payments_total
from errors import DatabaseError
from keyboards import Keyboards
from services import AIService, OpenRouterError, PaymentService
from utils import safe_format_message, split_message, truncate_for_log, validate_prompt

# Configure structured logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=config.LOG_LEVEL,
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


# ============ FSM States ============

class UserState(StatesGroup):
    """User conversation states."""
    waiting_for_question = State()


# ============ Bot Application ============

class BotApp:
    """Production-ready bot application."""

    def __init__(self) -> None:
        """Initialize bot application."""
        self.bot: Optional[Bot] = None
        self.dp: Optional[Dispatcher] = None
        self.db = Database()
        self.ai_service = AIService()
        self._typing_tasks: dict = {}
        self._shutdown_event = asyncio.Event()

    async def initialize(self) -> None:
        """Initialize all services."""
        logger.info("🚀 Initializing production bot...")

        # Initialize monitoring first
        monitoring.initialize()

        # Initialize cache
        await cache.initialize()

        # Initialize database
        await self.db.initialize()

        # Initialize AI service
        await self.ai_service.initialize()

        # Initialize bot
        self.bot = Bot(token=config.TELEGRAM_BOT_TOKEN, parse_mode="HTML")

        # Initialize dispatcher
        self.dp = Dispatcher()

        # Register shutdown handler
        signal.signal(signal.SIGTERM, lambda s, f: asyncio.create_task(self.shutdown()))
        signal.signal(signal.SIGINT, lambda s, f: asyncio.create_task(self.shutdown()))

        logger.info("✅ Bot initialized successfully")

    async def shutdown(self) -> None:
        """Graceful shutdown."""
        logger.info("🛑 Shutting down gracefully...")

        # Stop accepting new requests
        self._shutdown_event.set()

        # Cancel typing tasks
        for task in self._typing_tasks.values():
            if not task.done():
                task.cancel()

        # Close services
        if self.bot:
            await self.bot.session.close()
        await self.ai_service.close()
        await self.db.close()
        await cache.close()

        logger.info("✅ Shutdown complete")

    async def _typing_loop(self, chat_id: int, timeout: int = config.TYPING_MAX_DURATION) -> None:
        """Background typing indicator loop."""
        try:
            start_time = time.time()
            while time.time() - start_time < timeout:
                try:
                    await self.bot.send_chat_action(chat_id, "typing")
                    await asyncio.sleep(config.TYPING_REFRESH_INTERVAL)
                except Exception as e:
                    logger.warning(f"Typing action failed: {e}")
                    break
        except asyncio.CancelledError:
            pass
        finally:
            self._typing_tasks.pop(chat_id, None)

    async def start_typing_loop(self, chat_id: int) -> None:
        """Start typing indicator."""
        if chat_id in self._typing_tasks:
            task = self._typing_tasks[chat_id]
            if not task.done():
                task.cancel()

        task = asyncio.create_task(self._typing_loop(chat_id))
        self._typing_tasks[chat_id] = task

    async def stop_typing_loop(self, chat_id: int) -> None:
        """Stop typing indicator."""
        if chat_id in self._typing_tasks:
            task = self._typing_tasks[chat_id]
            if not task.done():
                task.cancel()

    # ============ COMMAND HANDLERS ============

    @count_requests("start")
    async def handle_start(self, message: Message) -> None:
        """Handle /start command with improved onboarding."""
        try:
            user_id = message.from_user.id
            username = message.from_user.username or f"user_{user_id}"

            # Check if banned
            if await self.db.is_user_banned(user_id):
                await message.answer("❌ Your account has been suspended. Contact support.")
                return

            # Get or create user
            user = await self.db.get_or_create_user(user_id, username)
            balance = user["questions_balance"]
            model = user["selected_model"]

            # Improved onboarding message
            text = (
                f"🤖 <b>Welcome to AI Chat Bot!</b>\n\n"
                f"Get instant answers from GPT-4, Claude, and more!\n\n"
                f"<b>Your Profile:</b>\n"
                f"💬 Questions: <code>{balance}</code>\n"
                f"🧠 Model: <code>{model}</code>\n\n"
            )

            if balance == 0:
                text += "🎁 <b>Get started:</b> Buy questions to unlock AI power! 👇"
            else:
                text += "✨ <b>Ready to go!</b> Ask your first question now! 👇"

            await message.answer(text, reply_markup=Keyboards.main_menu())
            logger.info(f"Start: user={user_id}, balance={balance}")

        except Exception as e:
            logger.exception(f"Error in start handler: {e}")
            monitoring.capture_exception(e, {"user_id": message.from_user.id})
            await message.answer("❌ Something went wrong. Please try again.")

    @count_requests("cancel")
    async def handle_cancel(self, message: Message, state: FSMContext) -> None:
        """Handle /cancel command."""
        try:
            await state.clear()
            await message.answer("❌ Cancelled.\n\nSend /start to return to main menu.")
            logger.info(f"Cancel: user={message.from_user.id}")

        except Exception as e:
            logger.exception(f"Error in cancel: {e}")

    async def handle_help(self, message: Message) -> None:
        """Handle /help command."""
        help_text = (
            "<b>🤖 AI Chat Bot - Help</b>\n\n"
            "<b>Commands:</b>\n"
            "/start - Main menu\n"
            "/help - Show this help\n"
            "/cancel - Cancel current action\n\n"
            "<b>Features:</b>\n"
            "🧠 Multiple AI models (GPT-4, Claude, etc.)\n"
            "⚡ Lightning-fast responses\n"
            "💳 Secure Telegram Stars payment\n"
            "🔒 Your data is private\n\n"
            "<b>How to use:</b>\n"
            "1. Buy questions with Telegram Stars\n"
            "2. Choose your preferred AI model\n"
            "3. Ask any question!\n\n"
            "<b>Need support?</b>\n"
            "Contact: @YourSupportBot"
        )
        await message.answer(help_text)

    # ============ CALLBACK HANDLERS ============

    @count_requests("choose_model")
    async def handle_choose_model(self, callback: CallbackQuery) -> None:
        """Handle model selection."""
        try:
            # Check rate limit
            if not await rate_limiter.check_callback_limit(callback.from_user.id):
                await callback.answer("⏱️ Please slow down", show_alert=True)
                return

            text = (
                "<b>🧠 Select an AI Model:</b>\n\n"
                "🚀 <b>GPT-4 Optimized</b> - Fastest & smartest\n"
                "🧠 <b>GPT-4.1 Turbo</b> - Best for complex tasks\n"
                "💬 <b>Claude 3.5 Sonnet</b> - Great for conversations\n"
                "🆓 <b>Free Model</b> - Basic answers\n"
            )
            await callback.message.edit_text(text, reply_markup=Keyboards.models())
            await callback.answer()

        except Exception as e:
            logger.exception(f"Error in choose_model: {e}")
            await callback.answer("❌ Failed to load models", show_alert=True)

    @count_requests("model_selected")
    async def handle_model_selected(self, callback: CallbackQuery) -> None:
        """Handle model selection confirmation."""
        user_id = callback.from_user.id
        model_key = callback.data.replace("model_", "")

        try:
            # Validate model
            config.get_model_by_key(model_key)
            await self.db.set_user_model(user_id, model_key)

            # Invalidate cache
            await cache.delete(cache.user_model_key(user_id))

            await callback.message.edit_text(
                f"✅ <b>Model updated to:</b> <code>{model_key}</code>\n\n"
                f"Use /start to continue."
            )
            await callback.answer("✅ Model updated!")
            logger.info(f"Model changed: user={user_id}, model={model_key}")

        except ValueError:
            logger.warning(f"Invalid model: {model_key}")
            await callback.answer("❌ Invalid model", show_alert=True)
        except Exception as e:
            logger.exception(f"Error in model_selected: {e}")
            await callback.answer("❌ Failed to update model", show_alert=True)

    @count_requests("buy_questions")
    async def handle_buy_questions(self, callback: CallbackQuery) -> None:
        """Handle buy questions button."""
        try:
            if not config.PAYMENTS_ENABLED:
                await callback.answer("💳 Payments are temporarily disabled", show_alert=True)
                return

            await callback.message.edit_text(
                "<b>⭐ Choose a payment package:</b>\n\n"
                "💎 <b>Best Value</b> - Save more with larger packages!",
                reply_markup=Keyboards.payment_packages()
            )
            await callback.answer()

        except Exception as e:
            logger.exception(f"Error in buy_questions: {e}")
            await callback.answer("❌ Failed to load packages", show_alert=True)

    @count_requests("payment_initiation")
    async def handle_payment_initiation(self, callback: CallbackQuery) -> None:
        """Handle payment package selection."""
        user_id = callback.from_user.id
        package = callback.data.replace("pay_", "")

        try:
            stars = config.get_stars_for_package(package)
            questions = config.get_questions_for_package(package)

            if not stars or not questions:
                await callback.answer("❌ Invalid package", show_alert=True)
                return

            payload = f"stars_{stars}"
            prices = [LabeledPrice(label=f"{stars} Telegram Stars", amount=stars)]

            await self.bot.send_invoice(
                chat_id=user_id,
                title="AI Questions Package",
                description=f"Get {questions} AI questions for {stars} Telegram Stars",
                payload=payload,
                provider_token="",
                currency="XTR",
                prices=prices,
                is_flexible=False,
            )
            await callback.answer()
            logger.info(f"Invoice sent: user={user_id}, package={package}")

        except Exception as e:
            logger.exception(f"Error in payment_initiation: {e}")
            monitoring.capture_exception(e, {"user_id": user_id})
            await callback.answer("❌ Failed to initiate payment", show_alert=True)

    @count_requests("ask_question")
    async def handle_ask_question(self, callback: CallbackQuery, state: FSMContext) -> None:
        """Handle ask question button."""
        user_id = callback.from_user.id

        try:
            # Check balance (with cache)
            balance_key = cache.user_balance_key(user_id)
            balance = await cache.get(balance_key)
            
            if balance is None:
                balance = await self.db.get_user_balance(user_id)
                await cache.set(balance_key, balance, ttl=60)

            if balance <= 0:
                await callback.message.edit_text(
                    "❌ <b>No Questions Remaining</b>\n\n"
                    "💡 Buy more questions to continue using AI!",
                    reply_markup=Keyboards.buy_button()
                )
                await callback.answer()
                return

            await state.set_state(UserState.waiting_for_question)
            await callback.message.edit_text(
                f"💬 <b>Ask Your Question</b>\n\n"
                f"Questions remaining: <code>{balance}</code>\n\n"
                f"💡 <i>Tip: Be specific for better answers!</i>"
            )
            await callback.answer()
            logger.info(f"Ask question state: user={user_id}")

        except Exception as e:
            logger.exception(f"Error in ask_question: {e}")
            await callback.answer("❌ Failed to load balance", show_alert=True)

    # ============ PAYMENT HANDLERS ============

    @count_requests("pre_checkout")
    async def handle_pre_checkout(self, pre_checkout: PreCheckoutQuery) -> None:
        """Handle pre-checkout validation."""
        try:
            payload = pre_checkout.invoice_payload

            if not PaymentService.validate_payment_payload(payload):
                logger.warning(f"Invalid payload: {payload}")
                await self.bot.answer_pre_checkout_query(
                    pre_checkout.id, ok=False, error_message="Invalid payment data"
                )
                return

            await self.bot.answer_pre_checkout_query(pre_checkout.id, ok=True)
            logger.info(f"Pre-checkout validated: user={pre_checkout.from_user.id}")

        except Exception as e:
            logger.exception(f"Error in pre_checkout: {e}")
            try:
                await self.bot.answer_pre_checkout_query(
                    pre_checkout.id, ok=False, error_message="Validation failed"
                )
            except Exception:
                pass

    @count_requests("successful_payment")
    async def handle_successful_payment(self, message: Message) -> None:
        """Handle successful payment with atomic transaction."""
        user_id = message.from_user.id
        payment = message.successful_payment

        try:
            payload = payment.invoice_payload
            telegram_charge_id = payment.telegram_payment_charge_id

            stars = PaymentService.extract_stars_amount(payload)
            if not stars:
                logger.error(f"Invalid payload: {payload}")
                await message.answer("❌ Payment error. Contact support.")
                payments_total.labels(status='error').inc()
                return

            questions = PaymentService.get_questions_for_stars(stars)

            # ATOMIC TRANSACTION - All or nothing
            try:
                payment_id = await self.db.record_payment(
                    user_id, stars, questions,
                    telegram_charge_id=telegram_charge_id,
                    status="pending"
                )
                
                # Complete transaction atomically
                new_balance = await self.db.complete_payment_transaction(
                    user_id, payment_id, questions
                )

                # Invalidate cache
                await cache.delete(cache.user_balance_key(user_id))

                # Success metrics
                payments_total.labels(status='success').inc()

                text = (
                    f"✅ <b>Payment Successful!</b>\n\n"
                    f"💰 {stars} Telegram Stars received\n"
                    f"➕ {questions} questions added\n"
                    f"📊 New balance: <code>{new_balance}</code> questions\n\n"
                    f"🚀 Use /start to ask questions!"
                )
                await message.answer(text)

                logger.info(
                    f"Payment completed: user={user_id}, stars={stars}, "
                    f"questions={questions}, charge_id={telegram_charge_id}"
                )

            except ValueError as e:
                # Duplicate payment
                logger.warning(f"Duplicate payment: {e}")
                await message.answer("✅ Payment already processed!")
                return

        except Exception as e:
            logger.exception(f"Payment error: {e}")
            monitoring.capture_exception(e, {"user_id": user_id})
            payments_total.labels(status='error').inc()
            await message.answer(
                "⚠️ Payment received but processing failed. "
                "Your balance will be updated shortly. Contact support if needed."
            )

    # ============ MESSAGE HANDLERS ============

    @count_requests("message")
    async def handle_message(self, message: Message, state: FSMContext) -> None:
        """Handle regular messages."""
        if not message.text:
            await message.answer("❌ Please send a text message")
            return

        user_id = message.from_user.id
        username = message.from_user.username or f"user_{user_id}"
        user_text = message.text.strip()

        # Check if banned
        if await self.db.is_user_banned(user_id):
            await message.answer("❌ Your account has been suspended.")
            return

        # Get/create user
        try:
            await self.db.get_or_create_user(user_id, username)
        except DatabaseError as e:
            logger.exception(f"Failed to get/create user: {e}")
            await message.answer("❌ Failed to load your profile")
            return

        current_state = await state.get_state()

        if current_state == UserState.waiting_for_question:
            await self._process_question(message, user_id, user_text, state)
        else:
            # Show stats
            try:
                user = await self.db.get_or_create_user(user_id, username)
                balance = user["questions_balance"]
                model = user["selected_model"]

                text = (
                    f"📊 <b>Your Stats:</b>\n"
                    f"Questions: <code>{balance}</code>\n"
                    f"Model: <code>{model}</code>\n\n"
                    f"Use /start to access menu"
                )
                await message.answer(text, reply_markup=Keyboards.main_menu())

            except Exception as e:
                logger.exception(f"Error in message handler: {e}")
                await message.answer("❌ Failed to load your profile")

    @track_time("process_question", {"model": "dynamic"})
    async def _process_question(
        self, message: Message, user_id: int, question_text: str, state: FSMContext
    ) -> None:
        """Process question with AI (production-grade)."""
        start_time = time.time()
        
        try:
            # Validate input
            try:
                validate_prompt(question_text)
            except ValueError as e:
                await message.answer(f"❌ {str(e)}")
                return

            # Check rate limits
            allowed, error_msg = await rate_limiter.check_user_question_limit(user_id)
            if not allowed:
                await message.answer(error_msg)
                return

            # Check balance
            balance = await self.db.get_user_balance(user_id)
            if balance <= 0:
                await message.answer("❌ No questions remaining. Use /start to buy more.")
                return

            # Get user's model
            model = await self.db.get_user_model(user_id)

            # Start typing
            await self.start_typing_loop(user_id)

            try:
                # Call AI with timeout
                logger.info(f"Processing: user={user_id}, model={model}, len={len(question_text)}")
                
                ai_response = await asyncio.wait_for(
                    self.ai_service.call_openrouter(question_text, model),
                    timeout=config.AI_REQUEST_TIMEOUT
                )

            except asyncio.TimeoutError:
                logger.error(f"AI timeout: user={user_id}")
                await message.answer(
                    "⏱️ Request timed out. Your question was not counted. Please try again."
                )
                questions_asked_total.labels(model=model, status='timeout').inc()
                return

            except OpenRouterError as e:
                logger.exception(f"OpenRouter error: {e}")
                await message.answer(
                    "❌ AI service is temporarily unavailable. Your question was not counted. "
                    "Please try again in a moment."
                )
                questions_asked_total.labels(model=model, status='error').inc()
                return

            finally:
                await self.stop_typing_loop(user_id)

            # Deduct balance (atomic)
            if not await self.db.deduct_user_balance(user_id, 1):
                await message.answer("❌ Insufficient balance. Please try again.")
                return

            # Invalidate cache
            await cache.delete(cache.user_balance_key(user_id))

            new_balance = await self.db.get_user_balance(user_id)

            # Format and send response
            formatted_text, parse_mode = safe_format_message(ai_response, safe=True)
            chunks = split_message(formatted_text)

            for chunk in chunks:
                await message.answer(chunk, parse_mode=parse_mode)

            await message.answer(
                f"📊 Questions remaining: <code>{new_balance}</code>",
                parse_mode="HTML"
            )

            # Log question
            processing_time = int((time.time() - start_time) * 1000)
            await self.db.log_question(
                user_id=user_id,
                model_used=model,
                prompt_length=len(question_text),
                response_length=len(ai_response),
                processing_time_ms=processing_time,
                success=True
            )

            # Metrics
            questions_asked_total.labels(model=model, status='success').inc()

            logger.info(f"Question processed: user={user_id}, time={processing_time}ms")

        except Exception as e:
            logger.exception(f"Unexpected error: {e}")
            monitoring.capture_exception(e, {"user_id": user_id})
            await message.answer("❌ An unexpected error occurred. Please try again.")

        finally:
            await state.clear()

    def register_handlers(self) -> None:
        """Register all handlers."""
        router = Router()

        # Commands
        router.message.register(self.handle_start, Command("start"))
        router.message.register(self.handle_cancel, Command("cancel"))
        router.message.register(self.handle_help, Command("help"))

        # Callbacks
        router.callback_query.register(self.handle_choose_model, F.data == "choose_model")
        router.callback_query.register(self.handle_model_selected, F.data.startswith("model_"))
        router.callback_query.register(self.handle_buy_questions, F.data == "buy_questions")
        router.callback_query.register(self.handle_payment_initiation, F.data.startswith("pay_"))
        router.callback_query.register(self.handle_ask_question, F.data == "ask_question")

        # Payments
        router.pre_checkout_query.register(self.handle_pre_checkout)
        router.message.register(self.handle_successful_payment, F.successful_payment)

        # Messages
        router.message.register(self.handle_message)

        self.dp.include_router(router)
        logger.info("✅ All handlers registered")

    async def run_webhook(self) -> None:
        """Run bot in webhook mode (PRODUCTION)."""
        logger.info("🚀 Starting webhook mode...")

        # Set webhook
        webhook_url = f"{config.WEBHOOK_DOMAIN}{config.WEBHOOK_PATH}"
        await self.bot.set_webhook(
            url=webhook_url,
            drop_pending_updates=True,
            secret_token=config.WEBHOOK_SECRET
        )

        # Create aiohttp app
        app = web.Application()

        # Setup webhook handler
        webhook_handler = SimpleRequestHandler(
            dispatcher=self.dp,
            bot=self.bot,
            secret_token=config.WEBHOOK_SECRET
        )
        webhook_handler.register(app, path=config.WEBHOOK_PATH)

        # Setup application
        setup_application(app, self.dp, bot=self.bot)

        # Add health check endpoint
        async def health_check(request):
            return web.json_response({"status": "healthy"})

        app.router.add_get("/health", health_check)

        # Run server
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', config.WEBHOOK_PORT)
        await site.start()

        logger.info(f"✅ Webhook server running on port {config.WEBHOOK_PORT}")
        logger.info(f"✅ Webhook URL: {webhook_url}")

        # Wait for shutdown
        await self._shutdown_event.wait()

    async def run_polling(self) -> None:
        """Run bot in polling mode (DEVELOPMENT ONLY)."""
        logger.warning("⚠️ Running in POLLING mode - NOT recommended for production!")
        await self.dp.start_polling(self.bot)


async def main() -> None:
    """Main entry point."""
    app = BotApp()
    
    try:
        await app.initialize()
        app.register_handlers()

        if config.WEBHOOK_ENABLED:
            await app.run_webhook()
        else:
            await app.run_polling()

    except KeyboardInterrupt:
        logger.info("Bot interrupted by user")
    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        monitoring.capture_exception(e)
        raise
    finally:
        await app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
