import discord
from discord import app_commands
from discord.ext import commands
import asyncpg
import os
import re
import asyncio
from datetime import datetime

# Bot setup
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Config
ECONOMY_MANAGER_ROLES = ["MR"]
BALANCE_CHANNEL_ID = int(os.getenv("BALANCE_CHANNEL_ID", 0))
USERS_PER_MESSAGE = 20

db_pool = None

# ==================== EVENTS ====================
@bot.event
async def on_ready():
    global db_pool
    print(f'✅ {bot.user} is online!')

    db_pool = await asyncpg.create_pool(os.getenv("DATABASE_URL"))

    async with db_pool.acquire() as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS balances (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                balance INTEGER DEFAULT 0
            )
        ''')
    print("✅ Connected to PostgreSQL")

    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(e)

    if BALANCE_CHANNEL_ID:
        bot.loop.create_task(balance_report_task())

async def balance_report_task():
    await bot.wait_until_ready()
    while True:
        await send_balance_report()
        await asyncio.sleep(6 * 3600)  # Every 6 hours

# ==================== HELPERS ====================
async def get_balance(user_id):
    async with db_pool.acquire() as conn:
        return await conn.fetchval("SELECT balance FROM balances WHERE user_id = $1", user_id) or 0

async def update_balance(user_id, username, amount):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO balances (user_id, username, balance)
            VALUES ($1, $2, $3)
            ON CONFLICT(user_id) DO UPDATE SET 
            balance = balances.balance + $3,
            username = $2
        """, user_id, username, amount)

async def get_all_balances():
    async with db_pool.acquire() as conn:
        return await conn.fetch("SELECT username, balance FROM balances WHERE balance > 0 ORDER BY balance DESC")

async def send_balance_report():
    """Send full balance report as multiple messages (no pagination)"""
    if not BALANCE_CHANNEL_ID:
        return
    channel = bot.get_channel(BALANCE_CHANNEL_ID)
    if not channel:
        return

    data = await get_all_balances()
    if not data:
        return

    # Split into chunks of USERS_PER_MESSAGE
    for i in range(0, len(data), USERS_PER_MESSAGE):
        chunk = data[i:i + USERS_PER_MESSAGE]
        desc = ""
        for idx, (name, bal) in enumerate(chunk, i + 1):
            desc += f"`{idx:2d}.` **{name}** — **{bal:,} silver**\n"

        page_num = (i // USERS_PER_MESSAGE) + 1
        total_pages = (len(data) + USERS_PER_MESSAGE - 1) // USERS_PER_MESSAGE

        embed = discord.Embed(
            title=f"📋 All Player Balances (Page {page_num}/{total_pages})",
            description=desc,
            color=0x00AAFF
        )
        embed.set_footer(text=f"Total users: {len(data)} | {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        await channel.send(embed=embed)
        await asyncio.sleep(0.5)  # Small delay to avoid rate limits

def has_economy_permission(interaction: discord.Interaction) -> bool:
    user_roles = [role.name for role in interaction.user.roles]
    return any(role_name in user_roles for role_name in ECONOMY_MANAGER_ROLES)

# ==================== COMMANDS ====================

@bot.tree.command(name="balance", description="Check balance")
@app_commands.describe(user="User (optional)")
async def balance(interaction: discord.Interaction, user: discord.Member = None):
    target = user or interaction.user
    bal = await get_balance(target.id)
    if target == interaction.user:
        await interaction.response.send_message(f"💰 **{target.name}**, your balance is **{bal:,} silver**.")
    else:
        await interaction.response.send_message(f"💰 **{target.name}** has **{bal:,} silver**.")

@bot.tree.command(name="give", description="Give silver")
@app_commands.describe(user="Recipient", amount="Amount")
async def give(interaction: discord.Interaction, user: discord.Member, amount: int):
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be positive!", ephemeral=True)
        return
    if await get_balance(interaction.user.id) < amount:
        await interaction.response.send_message("❌ Not enough silver!", ephemeral=True)
        return
    await update_balance(interaction.user.id, interaction.user.name, -amount)
    await update_balance(user.id, user.name, amount)
    await interaction.response.send_message(f"✅ Gave **{amount:,} silver** to **{user.name}**!")
    await send_balance_report()

@bot.tree.command(name="add", description="Add silver (MR only)")
@app_commands.describe(user="Target", amount="Amount")
async def add(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not has_economy_permission(interaction):
        await interaction.response.send_message("❌ MR only.", ephemeral=True)
        return
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be positive!", ephemeral=True)
        return
    await update_balance(user.id, user.name, amount)
    await interaction.response.send_message(f"✅ Added **{amount:,} silver** to **{user.name}**.")
    await send_balance_report()

@bot.tree.command(name="remove", description="Remove silver (MR only)")
@app_commands.describe(user="Target", amount="Amount")
async def remove(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not has_economy_permission(interaction):
        await interaction.response.send_message("❌ MR only.", ephemeral=True)
        return
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be positive!", ephemeral=True)
        return
    current = await get_balance(user.id)
    to_remove = min(amount, current)
    await update_balance(user.id, user.name, -to_remove)
    await interaction.response.send_message(f"✅ Removed **{to_remove:,} silver** from **{user.name}**.")
    await send_balance_report()

@bot.tree.command(name="clearbalance", description="Clear balance to 0 (MR only)")
@app_commands.describe(user="Target")
async def clearbalance(interaction: discord.Interaction, user: discord.Member):
    if not has_economy_permission(interaction):
        await interaction.response.send_message("❌ MR only.", ephemeral=True)
        return
    await update_balance(user.id, user.name, -await get_balance(user.id))
    await interaction.response.send_message(f"✅ Cleared **{user.name}**'s balance to 0.")
    await send_balance_report()

@bot.tree.command(name="allbalances", description="Show all balances")
async def allbalances(interaction: discord.Interaction):
    await interaction.response.defer()
    data = await get_all_balances()
    if not data:
        await interaction.followup.send("No balances yet!")
        return

    for i in range(0, len(data), USERS_PER_MESSAGE):
        chunk = data[i:i + USERS_PER_MESSAGE]
        desc = ""
        for idx, (name, bal) in enumerate(chunk, i + 1):
            desc += f"`{idx:2d}.` **{name}** — **{bal:,} silver**\n"

        page_num = (i // USERS_PER_MESSAGE) + 1
        total_pages = (len(data) + USERS_PER_MESSAGE - 1) // USERS_PER_MESSAGE

        embed = discord.Embed(
            title=f"📋 All Player Balances (Page {page_num}/{total_pages})",
            description=desc,
            color=0x00AAFF
        )
        embed.set_footer(text=f"Total users: {len(data)}")
        await interaction.followup.send(embed=embed)
        await asyncio.sleep(0.5)

@bot.tree.command(name="forcebalance", description="Force balance report to channel (MR only)")
async def forcebalance(interaction: discord.Interaction):
    if not has_economy_permission(interaction):
        await interaction.response.send_message("❌ MR only.", ephemeral=True)
        return
    await interaction.response.defer()
    await send_balance_report()
    await interaction.followup.send("✅ Full balance report sent to the channel!")

@bot.tree.command(name="massadd", description="Add silver to ALL mentioned users (MR only)")
@app_commands.describe(message_link="Discord message link", amount="Amount of silver")
async def massadd(interaction: discord.Interaction, message_link: str, amount: int):
    if not has_economy_permission(interaction):
        await interaction.response.send_message("❌ MR only.", ephemeral=True)
        return
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be positive!", ephemeral=True)
        return
    await interaction.response.defer()
    match = re.search(r'/channels/(\d+)/(\d+)/(\d+)', message_link)
    if not match:
        await interaction.followup.send("❌ Invalid link.")
        return
    _, channel_id, message_id = match.groups()
    try:
        channel = await bot.fetch_channel(int(channel_id))
        message = await channel.fetch_message(int(message_id))
        if not message.mentions:
            await interaction.followup.send("❌ No users mentioned.")
            return
        for user in message.mentions:
            await update_balance(user.id, user.name, amount)
        await interaction.followup.send(f"✅ Added **{amount:,} silver** to **{len(message.mentions)}** users!")
        await send_balance_report()
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {str(e)}")

@bot.tree.command(name="massremove", description="Remove silver from ALL mentioned users (MR only)")
@app_commands.describe(message_link="Discord message link", amount="Amount of silver to remove")
async def massremove(interaction: discord.Interaction, message_link: str, amount: int):
    if not has_economy_permission(interaction):
        await interaction.response.send_message("❌ MR only.", ephemeral=True)
        return
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be positive!", ephemeral=True)
        return
    await interaction.response.defer()
    match = re.search(r'/channels/(\d+)/(\d+)/(\d+)', message_link)
    if not match:
        await interaction.followup.send("❌ Invalid link.")
        return
    _, channel_id, message_id = match.groups()
    try:
        channel = await bot.fetch_channel(int(channel_id))
        message = await channel.fetch_message(int(message_id))
        if not message.mentions:
            await interaction.followup.send("❌ No users mentioned.")
            return
        removed = 0
        for user in message.mentions:
            current = await get_balance(user.id)
            to_remove = min(amount, current)
            if to_remove > 0:
                await update_balance(user.id, user.name, -to_remove)
                removed += 1
        await interaction.followup.send(f"✅ Removed **{amount:,} silver** from **{removed}** users!")
        await send_balance_report()
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {str(e)}")

@bot.tree.command(name="massclear", description="Clear balance for ALL mentioned users (MR only)")
@app_commands.describe(message_link="Discord message link")
async def massclear(interaction: discord.Interaction, message_link: str):
    if not has_economy_permission(interaction):
        await interaction.response.send_message("❌ MR only.", ephemeral=True)
        return
    await interaction.response.defer()
    match = re.search(r'/channels/(\d+)/(\d+)/(\d+)', message_link)
    if not match:
        await interaction.followup.send("❌ Invalid link.")
        return
    _, channel_id, message_id = match.groups()
    try:
        channel = await bot.fetch_channel(int(channel_id))
        message = await channel.fetch_message(int(message_id))
        if not message.mentions:
            await interaction.followup.send("❌ No users mentioned.")
            return
        cleared = 0
        for user in message.mentions:
            current = await get_balance(user.id)
            if current > 0:
                await update_balance(user.id, user.name, -current)
                cleared += 1
        await interaction.followup.send(f"✅ Cleared balance for **{cleared}** users.")
        await send_balance_report()
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {str(e)}")

@bot.tree.command(name="leaderboard", description="Show top 10 richest players")
async def leaderboard(interaction: discord.Interaction):
    data = await get_all_balances()
    if not data:
        await interaction.response.send_message("No data yet!")
        return
    desc = "\n".join([f"`{i:2d}.` **{name}** — **{bal:,} silver**" for i, (name, bal) in enumerate(data[:10], 1)])
    embed = discord.Embed(title="🏆 Richest Players", description=desc, color=0xFFD700)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="sync", description="Force sync commands (MR only)")
async def sync(interaction: discord.Interaction):
    if not has_economy_permission(interaction):
        await interaction.response.send_message("❌ MR only.", ephemeral=True)
        return
    await bot.tree.sync()
    await interaction.response.send_message("✅ Commands synced!")

# Run the bot
if __name__ == "__main__":
    TOKEN = os.getenv("DISCORD_TOKEN")
    if not TOKEN:
        TOKEN = input("Enter your Discord Bot Token: ")
    bot.run(TOKEN)
