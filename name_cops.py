import discord
from discord.ext import commands, tasks
from discord import app_commands
import requests
import asyncio
import random
import os
import json
import logging
from typing import Dict, List, Optional, Set
import time
import io

CONFIG = {
    'TOKEN': os.getenv('DISCORD_BOT_TOKEN'),
    'ALLOWED_ROLE': os.getenv('ALLOWED_ROLE', 'names'),
    'ALLOWED_GUILD_ID': int(os.getenv('ALLOWED_GUILD_ID', '0')),
    'HITS_CHANNEL_ID': int(os.getenv('HITS_CHANNEL_ID', '0')),
    'OWNER_ID': int(os.getenv('OWNER_ID', '0')),
    'BATCH_SIZE': int(os.getenv('BATCH_SIZE', '10')),
    'MAX_FILE_SIZE': int(os.getenv('MAX_FILE_SIZE', '1000000')),
    'REQUEST_DELAY_MIN': float(os.getenv('REQUEST_DELAY_MIN', '0.5')),
    'REQUEST_DELAY_MAX': float(os.getenv('REQUEST_DELAY_MAX', '2.0')),
    'DISCORD_MESSAGE_DELAY': float(os.getenv('DISCORD_MESSAGE_DELAY', '1.0'))
}

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

allowed_users: Set[int] = set()

def load_dictionary_words():
    words = set()
    dict_paths = [
        '/usr/share/dict/words',
        '/usr/dict/words',
        '/usr/share/dict/american-english',
        '/usr/share/dict/british-english'
    ]
    
    for path in dict_paths:
        try:
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    word = line.strip()
                    if word.isalpha() and 3 <= len(word) <= 12:
                        words.add(word.capitalize())
            if words:
                logger.info(f"Loaded {len(words)} words from {path}")
                return list(words)
        except FileNotFoundError:
            continue
    
    logger.warning("No system dictionary found. Attempting to download word list...")
    try:
        import urllib.request
        url = "https://raw.githubusercontent.com/dwyl/english-words/master/words_alpha.txt"
        response = urllib.request.urlopen(url, timeout=30)
        content = response.read().decode('utf-8')
        for line in content.splitlines():
            word = line.strip()
            if word.isalpha() and 3 <= len(word) <= 12:
                words.add(word.capitalize())
        if words:
            logger.info(f"Downloaded {len(words)} words from online dictionary")
            return list(words)
    except Exception as e:
        logger.error(f"Failed to download dictionary: {e}")
    
    logger.error("Failed to load dictionary. /gen command will not work properly")
    return []

DICTIONARY_WORDS = load_dictionary_words()

if not DICTIONARY_WORDS:
    logger.error("Failed to load dictionary. Bot may not work properly for /gen command")
else:
    logger.info(f"Total dictionary size: {len(DICTIONARY_WORDS)} words")

def generate_igns(count=500):
    if count >= len(DICTIONARY_WORDS):
        shuffled = DICTIONARY_WORDS.copy()
        random.shuffle(shuffled)
        return shuffled
    
    shuffled = DICTIONARY_WORDS.copy()
    random.shuffle(shuffled)
    return shuffled[:count]

def generate_user_agents(n=1000):
    browsers = ["Chrome", "Safari", "Edge", "Firefox"]
    platforms = [
        "Windows NT 10.0; Win64; x64",
        "Macintosh; Intel Mac OS X 13_5",
        "X11; Linux x86_64",
        "iPhone; CPU iPhone OS 17_5 like Mac OS X"
    ]
    user_agents = []
    for _ in range(n):
        browser = random.choice(browsers)
        platform = random.choice(platforms)
        if browser == "Chrome":
            ua = f"Mozilla/5.0 ({platform}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{random.randint(100,120)}.0.{random.randint(1000,6000)}.{random.randint(0,200)} Safari/537.36"
        elif browser == "Safari":
            ua = f"Mozilla/5.0 ({platform}) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/{random.randint(14,18)}.{random.randint(0,9)} Safari/605.1.15"
        elif browser == "Edge":
            ua = f"Mozilla/5.0 ({platform}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{random.randint(100,120)}.0.{random.randint(1000,6000)}.{random.randint(0,200)} Safari/537.36 Edg/{random.randint(100,120)}.0.{random.randint(1000,6000)}.{random.randint(0,200)}"
        else:
            ua = f"Mozilla/5.0 ({platform}; rv:{random.randint(90,118)}.0) Gecko/20100101 Firefox/{random.randint(90,118)}.0"
        user_agents.append(ua)
    return user_agents

class UserData:
    def __init__(self):
        self.file: List[str] = []
        self.loop: bool = False
        self.task: Optional[asyncio.Task] = None
        self.processed: int = 0
        self.total: int = 0
        self.last_activity: float = time.time()
        self._lock = asyncio.Lock()
    
    async def cleanup(self):
        async with self._lock:
            if self.task and not self.task.done():
                self.task.cancel()
                try:
                    await self.task
                except asyncio.CancelledError:
                    pass
            self.task = None
            self.file.clear()
            self.processed = 0
            self.total = 0

class DiscordRateLimiter:
    def __init__(self, delay: float = 1.0):
        self.delay = delay
        self.last_message = 0.0
    
    async def wait_if_needed(self):
        now = time.time()
        time_since_last = now - self.last_message
        if time_since_last < self.delay:
            await asyncio.sleep(self.delay - time_since_last)
        self.last_message = time.time()

USER_AGENTS = generate_user_agents(1000)
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
user_data: Dict[int, UserData] = {}
discord_rate_limiter = DiscordRateLimiter(CONFIG['DISCORD_MESSAGE_DELAY'])

def has_role(member, role_name):
    return any(role.name == role_name for role in member.roles)

def is_owner_or_has_permission(interaction: discord.Interaction) -> bool:
    if interaction.user.id == CONFIG['OWNER_ID']:
        return True
    if interaction.user.id in allowed_users:
        return True
    if interaction.guild and interaction.guild.id == CONFIG['ALLOWED_GUILD_ID']:
        return has_role(interaction.user, CONFIG['ALLOWED_ROLE'])
    return False

async def safe_send(channel, message: str):
    await discord_rate_limiter.wait_if_needed()
    
    if len(message) > 1900:
        chunks = [message[i:i+1900] for i in range(0, len(message), 1900)]
        for chunk in chunks:
            try:
                await channel.send(chunk)
                await discord_rate_limiter.wait_if_needed()
            except discord.HTTPException as e:
                logger.error(f"Failed to send message chunk: {e}")
    else:
        try:
            await channel.send(message)
        except discord.HTTPException as e:
            logger.error(f"Failed to send message: {e}")

async def check_username_availability(username: str) -> str:
    username = username.strip()
    if not username:
        return "Empty username skipped"
    
    for attempt in range(3):
        url = f"https://api-cops.criticalforce.fi/api/public/profile?usernames={username}"
        headers = {"User-Agent": random.choice(USER_AGENTS)}
        
        try:
            response = requests.get(url, headers=headers, timeout=10)
            logger.debug(f"Response status for {username}: {response.status_code}")
            
            if response.status_code == 200:
                try:
                    data = response.json()
                    if ("error" in data and data["error"] == 53) or (isinstance(data, list) and len(data) == 0):
                        return f"{username} ✓"
                    else:
                        return f"{username} ✗"
                except (json.JSONDecodeError, ValueError) as e:
                    logger.warning(f"JSON parsing error for {username}: {e}")
                    if attempt == 2:
                        return f"{username} - JSON parse error"
            elif response.status_code == 500:
                return f"{username} ✓"
            elif response.status_code == 403:
                logger.warning(f"403 Forbidden for {username} (attempt {attempt + 1})")
                if attempt < 2:
                    await asyncio.sleep(2.0)
                    continue
                return f"{username} - blocked (403)"
            else:
                logger.warning(f"Unexpected HTTP {response.status_code} for {username}")
                if attempt == 2:
                    return f"{username} - HTTP {response.status_code}"
                    
        except requests.Timeout:
            logger.warning(f"Timeout for {username} (attempt {attempt + 1})")
            if attempt == 2:
                return f"{username} - timeout"
        except requests.RequestException as e:
            logger.warning(f"Request error for {username}: {e}")
            if attempt == 2:
                return f"{username} - request error"
        
        if attempt < 2:
            await asyncio.sleep(1.0)
    
    return f"{username} - failed after 3 attempts"

@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user}")
    try:
        synced = await bot.tree.sync()
        logger.info(f"Synced {len(synced)} slash commands")
        for cmd in synced:
            logger.info(f"  - /{cmd.name}: {cmd.description}")
    except Exception as e:
        logger.error(f"Failed to sync slash commands: {e}")
    cleanup_inactive_users.start()

@tasks.loop(minutes=30)
async def cleanup_inactive_users():
    current_time = time.time()
    inactive_users = []
    
    for user_id, data in user_data.items():
        if current_time - data.last_activity > 3600:
            inactive_users.append(user_id)
    
    for user_id in inactive_users:
        logger.info(f"Cleaning up inactive user {user_id}")
        await user_data[user_id].cleanup()
        del user_data[user_id]

@bot.tree.command(name="add", description="Add a user to the allowed users list (Owner only)")
async def add_user_slash(interaction: discord.Interaction, user: discord.User):
    if interaction.user.id != CONFIG['OWNER_ID']:
        await interaction.response.send_message("Only the owner can use this command.", ephemeral=True)
        return
    
    allowed_users.add(user.id)
    logger.info(f"Owner added user {user} (ID: {user.id}) to allowed users")
    await interaction.response.send_message(f"Added {user.mention} to the allowed users list. They can now use the bot commands.")

@bot.tree.command(name="remove", description="Remove a user from the allowed users list (Owner only)")
async def remove_user_slash(interaction: discord.Interaction, user: discord.User):
    if interaction.user.id != CONFIG['OWNER_ID']:
        await interaction.response.send_message("Only the owner can use this command.", ephemeral=True)
        return
    
    if user.id in allowed_users:
        allowed_users.remove(user.id)
        logger.info(f"Owner removed user {user} (ID: {user.id}) from allowed users")
        await interaction.response.send_message(f"Removed {user.mention} from the allowed users list.")
    else:
        await interaction.response.send_message(f"{user.mention} was not in the allowed users list.", ephemeral=True)

@bot.tree.command(name="file", description="Upload a file of usernames to check")
async def upload_file_slash(interaction: discord.Interaction, attachment: discord.Attachment):
    if not is_owner_or_has_permission(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    
    if not attachment.filename.endswith(".txt"):
        await interaction.response.send_message("Only .txt files are supported.", ephemeral=True)
        return
    
    if attachment.size > CONFIG['MAX_FILE_SIZE']:
        await interaction.response.send_message(f"File too large. Maximum size: {CONFIG['MAX_FILE_SIZE']} bytes", ephemeral=True)
        return
    
    try:
        content = await attachment.read()
        usernames = content.decode('utf-8', errors='ignore').splitlines()
        usernames = [u.strip() for u in usernames if u.strip()]
        
        if interaction.user.id in user_data:
            await user_data[interaction.user.id].cleanup()
        
        user_data[interaction.user.id] = UserData()
        user_data[interaction.user.id].file = usernames
        user_data[interaction.user.id].total = len(usernames)
        user_data[interaction.user.id].last_activity = time.time()
        
        logger.info(f"User {interaction.user} uploaded file with {len(usernames)} usernames")
        await interaction.response.send_message(f"File uploaded. {len(usernames)} usernames stored.")
        
    except UnicodeDecodeError:
        await interaction.response.send_message("File encoding error. Please use UTF-8 encoding.", ephemeral=True)
    except Exception as e:
        logger.error(f"File upload error: {e}")
        await interaction.response.send_message("Error processing file.", ephemeral=True)

@bot.tree.command(name="on", description="Enable looping for continuous checking")
async def loop_on_slash(interaction: discord.Interaction):
    if not is_owner_or_has_permission(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    if interaction.user.id not in user_data:
        await interaction.response.send_message("Upload a file first using /file", ephemeral=True)
        return
    
    user_data[interaction.user.id].loop = True
    user_data[interaction.user.id].last_activity = time.time()
    logger.info(f"Looping enabled for user {interaction.user}")
    await interaction.response.send_message("Looping enabled for your account.")

@bot.tree.command(name="off", description="Disable looping")
async def loop_off_slash(interaction: discord.Interaction):
    if not is_owner_or_has_permission(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    if interaction.user.id not in user_data:
        await interaction.response.send_message("Upload a file first using /file", ephemeral=True)
        return
    
    user_data[interaction.user.id].loop = False
    user_data[interaction.user.id].last_activity = time.time()
    logger.info(f"Looping disabled for user {interaction.user}")
    await interaction.response.send_message("Looping disabled for your account.")

@bot.tree.command(name="kill", description="Stop the current checking process")
async def kill_task_slash(interaction: discord.Interaction):
    if interaction.user.id in user_data:
        await user_data[interaction.user.id].cleanup()
        await interaction.response.send_message("Your batch check has been stopped and cleaned up.")
        logger.info(f"Task killed for user {interaction.user}")
    else:
        await interaction.response.send_message("You have no running process to stop.", ephemeral=True)

@bot.tree.command(name="status", description="Check the status of your username checking")
async def check_status_slash(interaction: discord.Interaction):
    if interaction.user.id not in user_data:
        await interaction.response.send_message("No data found. Upload a file first using /file", ephemeral=True)
        return
    
    data = user_data[interaction.user.id]
    status = "Running" if data.task and not data.task.done() else "Stopped"
    progress = f"{data.processed}/{data.total}" if data.total > 0 else "0/0"
    loop_status = "Enabled" if data.loop else "Disabled"
    
    await interaction.response.send_message(f"**Status Report**\n"
                                          f"Status: {status}\n"
                                          f"Progress: {progress}\n"
                                          f"Looping: {loop_status}\n"
                                          f"Total usernames: {data.total}")

@bot.tree.command(name="start", description="Start checking usernames")
async def start_check_slash(interaction: discord.Interaction):
    if not is_owner_or_has_permission(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    if interaction.user.id not in user_data or not user_data[interaction.user.id].file:
        await interaction.response.send_message("Upload a file first using /file", ephemeral=True)
        return
    
    data = user_data[interaction.user.id]
    if data.task and not data.task.done():
        await interaction.response.send_message("You already have a running process. Use /kill to stop it first.", ephemeral=True)
        return

    hits_channel = bot.get_channel(CONFIG['HITS_CHANNEL_ID'])
    if hits_channel is None:
        await interaction.response.send_message("Could not find hits channel.", ephemeral=True)
        return

    async def process_usernames_loop():
        data = user_data[interaction.user.id]
        usernames = data.file
        logger.info(f"Starting username check for user {interaction.user}")
        
        while True:
            try:
                batch_messages = []
                for i, username in enumerate(usernames):
                    data.processed = i + 1
                    data.last_activity = time.time()
                    
                    if data.task and data.task.cancelled():
                        return
                    
                    result = await check_username_availability(username)
                    batch_messages.append(result)

                    if "✗" not in result and "error" not in result and "timeout" not in result and "failed" not in result:
                        try:
                            await safe_send(hits_channel, f"<@{CONFIG['OWNER_ID']}> {result}")
                        except Exception as e:
                            logger.error(f"Failed to send hit to private channel: {e}")

                    if len(batch_messages) >= CONFIG['BATCH_SIZE']:
                        try:
                            await safe_send(interaction.channel, "\n".join(batch_messages))
                            batch_messages = []
                        except Exception as e:
                            logger.error(f"Failed to send batch message: {e}")

                    await asyncio.sleep(random.uniform(CONFIG['REQUEST_DELAY_MIN'], CONFIG['REQUEST_DELAY_MAX']))

                if batch_messages:
                    try:
                        await safe_send(interaction.channel, "\n".join(batch_messages))
                    except Exception as e:
                        logger.error(f"Failed to send final batch: {e}")

                data.processed = 0
                
                if not data.loop:
                    logger.info(f"Looping disabled, stopping user {interaction.user}")
                    break
                    
            except asyncio.CancelledError:
                logger.info(f"Task cancelled for user {interaction.user}")
                return
            except Exception as e:
                logger.error(f"Error in processing loop for user {interaction.user}: {e}")
                try:
                    await safe_send(interaction.channel, f"Error occurred during processing: {str(e)}")
                except:
                    pass
                break

    task = asyncio.create_task(process_usernames_loop())
    data.task = task
    data.last_activity = time.time()
    
    await interaction.response.send_message(f"Started checking {len(data.file)} usernames. Looping: {data.loop}")
    logger.info(f"Task started for user {interaction.user}")

@bot.tree.command(name="gen", description="Generate valuable IGNs and get them as a .txt file")
async def generate_igns_command(interaction: discord.Interaction, amount: int = 500):
    if not is_owner_or_has_permission(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    
    if amount < 1:
        await interaction.response.send_message("Amount must be at least 1.", ephemeral=True)
        return
    if amount > 2000:
        await interaction.response.send_message("Maximum amount is 2000 IGNs.", ephemeral=True)
        return
    
    await interaction.response.defer()
    
    try:
        igns = generate_igns(amount)
        file_content = "\n".join(igns)
        file_bytes = io.BytesIO(file_content.encode('utf-8'))
        file_bytes.seek(0)
        discord_file = discord.File(file_bytes, filename=f"valuable_igns_{len(igns)}.txt")
        
        await interaction.followup.send(
            content=f"Generated **{len(igns)}** unique valuable IGNs",
            file=discord_file
        )
        logger.info(f"User {interaction.user} generated {len(igns)} IGNs")
        
    except Exception as e:
        logger.error(f"Error generating IGNs: {e}")
        await interaction.followup.send("An error occurred while generating IGNs.", ephemeral=True)

bot.run(CONFIG['TOKEN'])