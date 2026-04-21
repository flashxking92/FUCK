#!/usr/bin/env python3
"""
msg1.py - ULTIMATE INSTAGRAM MESSAGE SENDER
Fully integrated with ig.py bot | Unlimited sending | Auto-stop support
Supports: /threads (tabs), /stop, /autostop, /switch, /pair rotation
"""

import argparse
import json
import os
import sys
import time
import random
import threading
import queue
import logging
import signal
import hashlib
import shutil
import subprocess
from datetime import datetime
from typing import List, Dict, Optional, Any
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

# Playwright imports
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [%(levelname)s] - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# ================= CONFIGURATION =================
MAX_RETRIES = 3
RETRY_DELAY = 1
DEFAULT_MESSAGE_DELAY = 0.3  # 0.3 seconds between messages

# Anti-detection user agents
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
]

# Message input selectors (tested and working)
INPUT_SELECTORS = [
    'div[contenteditable="true"][role="textbox"]',
    'textarea[placeholder*="message"]',
    'div[contenteditable="true"]',
    '._akhn',
    '[contenteditable="true"]',
    'div[class*="message"] div[contenteditable="true"]',
    'div[role="textbox"]',
    'div[class*="input"] [contenteditable="true"]',
]


@dataclass
class MessageSender:
    """Ultimate Instagram message sender with full bot integration"""
    username: str
    password: str
    thread_url: str
    messages: List[str]
    num_tabs: int  # This is controlled by /threads command
    headless: bool
    storage_state: Optional[str] = None
    delay: float = DEFAULT_MESSAGE_DELAY
    infinite: bool = True  # Unlimited sending by default
    target_id: str = None
    comma_separated: bool = False
    
    def __post_init__(self):
        self.sent_count = 0
        self.failed_count = 0
        self.lock = threading.Lock()
        self.message_queue = queue.Queue()
        self.running = True
        self.stop_requested = False
        self.auto_stop_time = 0  # 0 = disabled
        self.start_time = 0
        self.pair_index = 0
        self.pair_list = []
        
        # Generate target ID from thread URL
        if not self.target_id:
            self.target_id = hashlib.md5(self.thread_url.encode()).hexdigest()[:16]
        
        # Load messages into queue
        self._load_messages_to_queue()
        
        # Stats tracking
        self.total_messages = self.message_queue.qsize()
        self.status_file = f"status_{self.target_id}.json"
        
        # Setup signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        
        logger.info(f"📦 Initialized sender for {self.username}")
        logger.info(f"📨 Total messages available: {self.total_messages}")
        logger.info(f"🖥️ Tabs (threads): {self.num_tabs}")
        logger.info(f"⚡ Delay: {self.delay}s")
        logger.info(f"🔄 Infinite mode: {self.infinite}")
    
    def _signal_handler(self, signum, frame):
        """Handle shutdown signals from bot"""
        logger.info("\n🛑 Received stop signal, stopping gracefully...")
        self.stop_requested = True
        self.running = False
        self._update_status("stopped_by_command")
    
    def _load_messages_to_queue(self):
        """Load messages to queue for sending"""
        # Clear existing queue
        while not self.message_queue.empty():
            try:
                self.message_queue.get_nowait()
            except:
                break
        
        # Load messages based on mode
        if self.comma_separated:
            # Already split by comma in load_messages
            for msg in self.messages:
                if msg and msg.strip():
                    self.message_queue.put(msg.strip())
        else:
            # Each line is a separate message
            for msg in self.messages:
                if msg and msg.strip():
                    self.message_queue.put(msg.strip())
        
        # For infinite mode, we need to cycle messages
        if self.infinite and self.message_queue.qsize() > 0:
            # Make a copy of messages for cycling
            self.message_cycle = list(self.message_queue.queue)
            self.cycle_index = 0
    
    def _update_status(self, status: str):
        """Update status file for bot monitoring"""
        try:
            status_data = {
                "status": status,
                "sent": self.sent_count,
                "failed": self.failed_count,
                "total": self.total_messages,
                "running": self.running,
                "target_id": self.target_id,
                "thread_url": self.thread_url,
                "username": self.username,
                "tabs": self.num_tabs,
                "delay": self.delay,
                "infinite": self.infinite,
                "last_update": time.time(),
                "auto_stop_time": self.auto_stop_time,
                "start_time": self.start_time
            }
            with open(self.status_file, 'w') as f:
                json.dump(status_data, f, indent=2)
        except Exception as e:
            logger.debug(f"Status update failed: {e}")
    
    def set_auto_stop(self, minutes: int):
        """Set auto-stop timer (called from bot /autostop command)"""
        if minutes > 0:
            self.auto_stop_time = time.time() + (minutes * 60)
            logger.info(f"⏰ Auto-stop set for {minutes} minutes from now")
            self._update_status("running_with_autostop")
        else:
            self.auto_stop_time = 0
            logger.info("⏰ Auto-stop disabled")
            self._update_status("running")
    
    def set_pair_info(self, pair_list: List[str], current_index: int):
        """Set pair rotation info (called from bot when switching)"""
        self.pair_list = pair_list
        self.pair_index = current_index
        logger.info(f"🔄 Pair rotation set: {len(pair_list)} accounts, current index: {current_index}")
    
    def get_next_message(self) -> Optional[str]:
        """Get next message to send (handles infinite cycling)"""
        if not self.infinite:
            # Limited mode: just get from queue
            try:
                return self.message_queue.get(timeout=0.1)
            except queue.Empty:
                return None
        
        # Infinite mode: cycle through messages
        if hasattr(self, 'message_cycle') and self.message_cycle:
            msg = self.message_cycle[self.cycle_index % len(self.message_cycle)]
            self.cycle_index += 1
            return msg
        
        # Fallback: try queue
        try:
            msg = self.message_queue.get(timeout=0.1)
            self.message_queue.put(msg)  # Put back for next cycle
            return msg
        except queue.Empty:
            return None
    
    def find_message_input(self, page) -> Any:
        """Find message input with multiple fallback strategies"""
        # Strategy 1: Try standard selectors
        for selector in INPUT_SELECTORS:
            try:
                element = page.locator(selector).first
                if element.count() > 0 and element.is_visible():
                    logger.debug(f"Found input with selector: {selector}")
                    return element
            except:
                continue
        
        # Strategy 2: JavaScript to find any contenteditable
        try:
            js_find = """
                () => {
                    const elements = document.querySelectorAll('[contenteditable="true"]');
                    for (const el of elements) {
                        if (el.isConnected && el.offsetParent !== null) {
                            return true;
                        }
                    }
                    return false;
                }
            """
            if page.evaluate(js_find):
                element = page.locator('[contenteditable="true"]').first
                if element.count() > 0:
                    logger.debug("Found input via JS contenteditable")
                    return element
        except:
            pass
        
        # Strategy 3: Click on chat area to activate input
        try:
            chat_area = page.locator('div[role="main"]').first
            if chat_area.count() > 0:
                chat_area.click()
                time.sleep(1)
                for selector in INPUT_SELECTORS[:3]:
                    try:
                        element = page.locator(selector).first
                        if element.count() > 0:
                            return element
                    except:
                        continue
        except:
            pass
        
        return None
    
    def send_message(self, page, msg: str, tab_id: int) -> bool:
        """Send a single message with robust methods"""
        try:
            # Find message input
            message_input = self.find_message_input(page)
            if not message_input:
                logger.error(f"Tab {tab_id}: Could not find message input")
                return False
            
            # Click to focus
            try:
                message_input.click()
                time.sleep(0.2)
            except:
                pass
            
            # Clear any existing text
            try:
                page.keyboard.press("Control+A")
                page.keyboard.press("Backspace")
                time.sleep(0.05)
            except:
                pass
            
            # Method 1: Fill and press Enter (fastest)
            try:
                message_input.fill(msg)
                time.sleep(0.1)
                page.keyboard.press("Enter")
                time.sleep(self.delay)
                return True
            except Exception as e:
                logger.debug(f"Tab {tab_id}: Fill method failed: {e}")
            
            # Method 2: Type character by character (most reliable)
            try:
                for char in msg:
                    page.keyboard.type(char, delay=0.01)
                    time.sleep(0.005)
                time.sleep(0.2)
                page.keyboard.press("Enter")
                time.sleep(self.delay)
                return True
            except Exception as e:
                logger.debug(f"Tab {tab_id}: Type method failed: {e}")
            
            # Method 3: JavaScript injection (stealth)
            try:
                js_send = f"""
                    () => {{
                        const inputs = document.querySelectorAll('[contenteditable="true"]');
                        for (const input of inputs) {{
                            if (input.isConnected) {{
                                input.focus();
                                input.innerText = '';
                                input.innerText = `{msg.replace("`", "\\`")}`;
                                input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                                input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                                
                                const event = new KeyboardEvent('keydown', {{
                                    key: 'Enter',
                                    code: 'Enter',
                                    keyCode: 13,
                                    which: 13,
                                    bubbles: true
                                }});
                                input.dispatchEvent(event);
                                return true;
                            }}
                        }}
                        return false;
                    }}
                """
                result = page.evaluate(js_send)
                if result:
                    time.sleep(self.delay)
                    return True
            except Exception as e:
                logger.debug(f"Tab {tab_id}: JS method failed: {e}")
            
            return False
            
        except Exception as e:
            logger.error(f"Tab {tab_id}: Send error: {e}")
            return False
    
    def wait_for_ready(self, page, tab_id: int) -> bool:
        """Wait for page to be ready for messaging"""
        max_wait = 45
        start = time.time()
        
        while time.time() - start < max_wait and self.running:
            try:
                url = page.url.lower()
                
                # Check for login page
                if 'login' in url or 'accounts/login' in url:
                    logger.error(f"Tab {tab_id}: Not logged in!")
                    return False
                
                # Check for challenge/checkpoint
                if 'challenge' in url or 'checkpoint' in url:
                    logger.error(f"Tab {tab_id}: Challenge/checkpoint required!")
                    return False
                
                # Check if message area is visible
                msg_area = page.locator('div[role="main"]').first
                if msg_area.count() > 0 and msg_area.is_visible():
                    logger.debug(f"Tab {tab_id}: Page ready")
                    return True
                
                # Check for any input
                for selector in INPUT_SELECTORS[:3]:
                    element = page.locator(selector).first
                    if element.count() > 0 and element.is_visible():
                        logger.debug(f"Tab {tab_id}: Input found")
                        return True
                
                time.sleep(1)
                
            except Exception as e:
                logger.debug(f"Tab {tab_id}: Wait error: {e}")
                time.sleep(1)
        
        logger.error(f"Tab {tab_id}: Page not ready after {max_wait}s")
        return False
    
    def send_loop(self, tab_id: int, browser, context, page) -> Dict[str, int]:
        """Continuous send loop for a tab"""
        sent = 0
        failed = 0
        consecutive_failures = 0
        
        # Wait for page to be ready
        if not self.wait_for_ready(page, tab_id):
            logger.error(f"Tab {tab_id}: Page not ready, exiting")
            return {"sent": 0, "failed": 0}
        
        logger.info(f"Tab {tab_id}: Starting send loop")
        
        while self.running and not self.stop_requested:
            # Check auto-stop timer
            if self.auto_stop_time > 0 and time.time() >= self.auto_stop_time:
                logger.info(f"Tab {tab_id}: Auto-stop reached, stopping...")
                self.running = False
                break
            
            # Get next message
            msg = self.get_next_message()
            if not msg:
                if not self.infinite:
                    logger.info(f"Tab {tab_id}: No more messages")
                    break
                else:
                    # In infinite mode, we should always have a message
                    time.sleep(0.1)
                    continue
            
            # Send with retry
            success = False
            for attempt in range(MAX_RETRIES):
                if not self.running or self.stop_requested:
                    break
                
                try:
                    if self.send_message(page, msg, tab_id):
                        success = True
                        break
                    else:
                        time.sleep(RETRY_DELAY)
                except Exception as e:
                    logger.debug(f"Tab {tab_id}: Attempt {attempt+1} failed: {e}")
                    time.sleep(RETRY_DELAY)
            
            if success:
                sent += 1
                with self.lock:
                    self.sent_count += 1
                consecutive_failures = 0
                
                # Log progress
                if self.sent_count % 50 == 0:
                    logger.info(f"📊 Progress: {self.sent_count} messages sent")
                    self._update_status("running")
            else:
                failed += 1
                with self.lock:
                    self.failed_count += 1
                consecutive_failures += 1
                logger.warning(f"Tab {tab_id}: Failed to send: {msg[:30]}...")
            
            # Check if page is still responsive
            if consecutive_failures > 15:
                logger.warning(f"Tab {tab_id}: Too many failures, refreshing page...")
                try:
                    page.reload(wait_until='domcontentloaded', timeout=30000)
                    time.sleep(3)
                    if not self.wait_for_ready(page, tab_id):
                        logger.error(f"Tab {tab_id}: Page unrecoverable")
                        break
                    consecutive_failures = 0
                except Exception as e:
                    logger.error(f"Tab {tab_id}: Page refresh failed: {e}")
                    break
        
        return {"sent": sent, "failed": failed}
    
    def run_tab(self, tab_id: int) -> Dict[str, Any]:
        """Run a single browser tab"""
        browser = None
        context = None
        
        try:
            with sync_playwright() as p:
                # Launch browser with stealth settings
                browser = p.chromium.launch(
                    headless=self.headless,
                    args=[
                        '--no-sandbox',
                        '--disable-dev-shm-usage',
                        '--disable-gpu',
                        '--disable-setuid-sandbox',
                        '--no-first-run',
                        '--no-zygote',
                        '--disable-blink-features=AutomationControlled',
                        '--window-size=1280,800',
                        '--disable-web-security',
                        '--disable-features=IsolateOrigins,site-per-process',
                        '--disable-background-timer-throttling',
                        '--disable-backgrounding-occluded-windows',
                    ]
                )
                
                # Create context with realistic profile
                context = browser.new_context(
                    user_agent=random.choice(USER_AGENTS),
                    viewport={'width': random.randint(1200, 1400), 'height': random.randint(700, 900)},
                    locale='en-US',
                    timezone_id='America/New_York',
                    device_scale_factor=1,
                )
                
                # Anti-detection script
                context.add_init_script("""
                    // Remove automation traces
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
                    Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
                    Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
                    
                    // Add chrome object
                    window.chrome = { runtime: {} };
                    
                    // Override permissions
                    const originalQuery = window.navigator.permissions.query;
                    window.navigator.permissions.query = (parameters) => (
                        parameters.name === 'notifications' ?
                        Promise.resolve({ state: 'denied' }) :
                        originalQuery(parameters)
                    );
                    
                    // Override connection
                    Object.defineProperty(navigator, 'connection', {
                        get: () => ({
                            rtt: 50,
                            downlink: 10,
                            effectiveType: '4g',
                            saveData: false,
                        })
                    });
                """)
                
                # Load cookies from storage state
                if self.storage_state and os.path.exists(self.storage_state):
                    try:
                        with open(self.storage_state, 'r') as f:
                            state = json.load(f)
                        if state.get('cookies'):
                            context.add_cookies(state['cookies'])
                            logger.info(f"Tab {tab_id}: Loaded {len(state['cookies'])} cookies")
                    except Exception as e:
                        logger.warning(f"Tab {tab_id}: Cookie load failed: {e}")
                
                # Create page
                page = context.new_page()
                page.set_default_timeout(30000)
                
                # Navigate to thread
                logger.info(f"Tab {tab_id}: Navigating to {self.thread_url[:60]}...")
                page.goto(self.thread_url, wait_until='domcontentloaded', timeout=45000)
                
                # Random human-like delay
                time.sleep(random.uniform(1.5, 3))
                
                # Run send loop
                result = self.send_loop(tab_id, browser, context, page)
                
                logger.info(f"Tab {tab_id}: Completed - Sent: {result['sent']}, Failed: {result['failed']}")
                browser.close()
                return result
                
        except Exception as e:
            logger.error(f"Tab {tab_id}: Fatal error: {e}")
            if browser:
                try:
                    browser.close()
                except:
                    pass
            return {"sent": 0, "failed": 0, "error": str(e)}
    
    def run(self) -> Dict[str, Any]:
        """Run the message sender with multiple tabs"""
        self.start_time = time.time()
        self._update_status("starting")
        
        logger.info("=" * 70)
        logger.info("💀 ULTIMATE INSTAGRAM MESSAGE SENDER 💀")
        logger.info("=" * 70)
        logger.info(f"👤 Account: {self.username}")
        logger.info(f"🔗 Target: {self.thread_url[:80]}...")
        logger.info(f"📨 Messages: {self.total_messages}")
        logger.info(f"🖥️ Tabs (threads): {self.num_tabs}")
        logger.info(f"⚡ Delay: {self.delay}s per message")
        logger.info(f"🔄 Infinite mode: {self.infinite}")
        logger.info(f"🎭 Headless: {self.headless}")
        if self.auto_stop_time > 0:
            remaining = (self.auto_stop_time - time.time()) / 60
            logger.info(f"⏰ Auto-stop: {remaining:.0f} minutes remaining")
        logger.info("=" * 70)
        logger.info("🚀 SENDING STARTED - Press Ctrl+C to stop")
        logger.info("=" * 70)
        
        # Run tabs
        results = []
        with ThreadPoolExecutor(max_workers=self.num_tabs) as executor:
            futures = [executor.submit(self.run_tab, i + 1) for i in range(self.num_tabs)]
            for future in as_completed(futures):
                try:
                    result = future.result(timeout=3600)  # 1 hour timeout per tab
                    results.append(result)
                except Exception as e:
                    logger.error(f"Tab failed: {e}")
                    results.append({"sent": 0, "failed": 0, "error": str(e)})
        
        # Calculate totals
        total_sent = sum(r.get('sent', 0) for r in results)
        total_failed = sum(r.get('failed', 0) for r in results)
        elapsed_time = time.time() - self.start_time
        
        # Calculate speed
        speed = total_sent / elapsed_time if elapsed_time > 0 else 0
        
        logger.info("=" * 70)
        logger.info("✅ SENDING COMPLETED")
        logger.info("=" * 70)
        logger.info(f"📨 Total Sent: {total_sent}")
        logger.info(f"❌ Total Failed: {total_failed}")
        logger.info(f"⏱️ Time: {elapsed_time:.2f}s")
        logger.info(f"⚡ Speed: {speed:.2f} msg/s")
        logger.info("=" * 70)
        
        self._update_status("completed")
        
        return {
            "success": total_sent > 0,
            "sent": total_sent,
            "failed": total_failed,
            "total": self.total_messages,
            "elapsed": elapsed_time,
            "speed": speed,
            "tabs": self.num_tabs
        }


def load_messages_from_file(filename: str, comma_separated: bool = False) -> List[str]:
    """Load messages from file with proper encoding"""
    messages = []
    
    if not os.path.exists(filename):
        logger.error(f"File not found: {filename}")
        return messages
    
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            content = f.read().strip()
        
        if not content:
            logger.error("File is empty")
            return messages
        
        if comma_separated:
            # Split by comma, handle quoted values
            import re
            parts = [p.strip() for p in content.split(',') if p.strip()]
            messages = parts
        else:
            # Split by newline, ignore empty lines
            messages = [line.strip() for line in content.split('\n') if line.strip()]
        
        logger.info(f"✓ Loaded {len(messages)} messages from {filename}")
        
        # Show preview
        if messages:
            preview = messages[0][:50] + "..." if len(messages[0]) > 50 else messages[0]
            logger.info(f"  Preview: {preview}")
        
    except Exception as e:
        logger.error(f"Failed to load messages: {e}")
    
    return messages


def parse_arguments():
    """Parse command line arguments - compatible with ig.py bot"""
    parser = argparse.ArgumentParser(
        description='Ultimate Instagram Message Sender - Integrated with ig.py bot',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic usage (matches ig.py /attack command)
  python msg1.py --username johndoe --password pass123 --thread-url "https://www.instagram.com/direct/t/123456789/" --names messages.txt --tabs 3
  
  # With auto-stop (matches ig.py /autostop command)
  python msg1.py --username johndoe --password pass123 --thread-url "..." --names messages.txt --tabs 3 --auto-stop 30
  
  # Unlimited mode (default, never stops unless stopped)
  python msg1.py --username johndoe --password pass123 --thread-url "..." --names messages.txt --tabs 5 --delay 0.2
  
  # With storage state (from ig.py login)
  python msg1.py --username johndoe --password pass123 --thread-url "..." --names messages.txt --storage-state sessions/123_johndoe_state.json --tabs 3
        """
    )
    parser.add_argument('--username', required=True, help='Instagram username')
    parser.add_argument('--password', required=True, help='Instagram password')
    parser.add_argument('--thread-url', required=True, help='Thread URL to send messages to')
    parser.add_argument('--names', required=True, help='File containing messages')
    parser.add_argument('--tabs', type=int, default=1, help='Number of browser tabs (controlled by /threads command)')
    parser.add_argument('--headless', type=str, default='true', help='Run in headless mode (true/false)')
    parser.add_argument('--storage-state', help='Path to storage state file (from ig.py session)')
    parser.add_argument('--comma', action='store_true', help='Treat messages as comma-separated')
    parser.add_argument('--delay', type=float, default=0.3, help='Delay between messages in seconds')
    parser.add_argument('--limit', type=int, default=0, help='Message limit (0=unlimited, for /autostop compatibility)')
    parser.add_argument('--auto-stop', type=int, default=0, help='Auto-stop after N minutes (from /autostop command)')
    parser.add_argument('--target-id', help='Target ID for tracking')
    return parser.parse_args()


def main():
    """Main entry point"""
    args = parse_arguments()
    
    # Load messages
    messages = load_messages_from_file(args.names, args.comma)
    
    if not messages:
        logger.error("❌ No messages to send!")
        sys.exit(1)
    
    # Validate tab count (from /threads command)
    num_tabs = max(1, min(args.tabs, 10))
    
    # Convert headless string to bool
    headless = args.headless.lower() == 'true'
    
    # Check for auto-stop (from /autostop command)
    auto_stop_minutes = args.auto_stop
    
    # Check if limit is set (non-infinite mode)
    infinite_mode = args.limit == 0
    
    # Create and run sender
    sender = MessageSender(
        username=args.username,
        password=args.password,
        thread_url=args.thread_url,
        messages=messages,
        num_tabs=num_tabs,
        headless=headless,
        storage_state=args.storage_state,
        delay=args.delay,
        infinite=infinite_mode,
        target_id=args.target_id,
        comma_separated=args.comma
    )
    
    # Set auto-stop if specified
    if auto_stop_minutes > 0:
        sender.set_auto_stop(auto_stop_minutes)
    
    try:
        result = sender.run()
        
        # Return exit code based on success
        if result['sent'] > 0:
            logger.info("🎉 Message sending completed successfully!")
            sys.exit(0)
        else:
            logger.error("💀 No messages were sent!")
            sys.exit(1)
            
    except KeyboardInterrupt:
        logger.info("\n🛑 Stopped by user or bot command")
        sender._update_status("stopped_by_command")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sender._update_status("error")
        sys.exit(1)


if __name__ == "__main__":
    main()