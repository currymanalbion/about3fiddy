import discord
from discord import app_commands, ui
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
        bot.loop.create_task(hourly_balance_report())

async def hourly_balance_report():
    await bot.wait_until_ready()
    while True:
        if BALANCE_CHANNEL_ID:
            channel = bot.get_channel(BALANCE_CHANNEL_ID)
            if channel:
                data = await get_all_balances()
                if data:
                    desc = ""
                    for i, (name, bal) in enumerate(data[:15], 1):
                        desc += f"`{i:2d}.` **{name}** — **{bal:,} silver**\n"
                    embed = discord.Embed(title="📊 Hourly Balance Report", description=desc, color=0x00AAFF)
                    embed.set_footer(text=f"Total users: {len(data)} | {datetime.now().strftime('%Y-%m-%d %H:%M')}")
                    await channel.send(embed=embed)
        await asyncio.sleep(3600)

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

def has_economy_permission(interaction: discord.Interaction) -> bool:
    user_roles = [role.name for role in interaction.user.roles]
    return any(role_name in user_roles for role_name in ECONOMY_MANAGER_ROLES)

# ==================== PAGINATOR ====================
class BalancePaginator(ui.View):
    def __init__(self, interaction: discord.Interaction, data, per_page=10):
        super().__init__(timeout=300)
        self.interaction = interaction
        self.data = data
        self.per_page = per_page
        self.current_page = 0

    def get_embed(self):
        start = self.current_page * self.per_page
        end = start + self.per_page
        page_data = self.data[start:end]

        desc = ""
        for i, (name, bal) in enumerate(page_data, start + 1):
            desc += f"`{i:2d}.` **{name}** — **{bal:,} silver**\n"

        total_pages = (len(self.data) + self.per_page - 1) // self.per_page
        embed = discord.Embed(title="📋 All Player Balances", description=desc or "No balances yet.", color=0x00AAFF)
        embed.set_footer(text=f"Page {self.current_page + 1}/{total_pages} • Total: {len(self.data)}")
        return embed

    @ui.button(label="◀️", style=discord.ButtonStyle.gray)
    async def previous(self, interaction: discord.Interaction, button: ui.Button):
        if self.current_page > 0:
            self.current_page -= 1
            await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @ui.button(label="▶️", style=discord.ButtonStyle.gray)
    async def next(self, interaction: discord.Interaction, button: ui.Button):
        if (self.current_page + 1) * self.per_page < len(self.data):
            self.current_page += 1
            await interaction.response.edit_message(embed=self.get_embed(), view=self)

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

@bot.tree.command(name="clearbalance", description="Clear balance to 0 (MR only)")
@app_commands.describe(user="Target")
async def clearbalance(interaction: discord.Interaction, user: discord.Member):
    if not has_economy_permission(interaction):
        await interaction.response.send_message("❌ MR only.", ephemeral=True)
        return
    await update_balance(user.id, user.name, -await get_balance(user.id))
    await interaction.response.send_message(f"✅ Cleared **{user.name}**'s balance to 0.")

@bot.tree.command(name="allbalances", description="Show all balances (paginated)")
async def allbalances(interaction: discord.Interaction):
    data = await get_all_balances()
    if not data:
        await interaction.response.send_message("No balances yet!")
        return
    view = BalancePaginator(interaction, data)
    await interaction.response.send_message(embed=view.get_embed(), view=view)

@bot.tree.command(name="forcebalance", description="Force balance report (MR only)")
async def forcebalance(interaction: discord.Interaction):
    if not has_economy_permission(interaction):
        await interaction.response.send_message("❌ MR only.", ephemeral=True)
        return
    await interaction.response.defer()
    if not BALANCE_CHANNEL_ID:
        await interaction.followup.send("❌ BALANCE_CHANNEL_ID not set.")
        return
    channel = bot.get_channel(BALANCE_CHANNEL_ID)
    if not channel:
        await interaction.followup.send("❌ Channel not found.")
        return
    data = await get_all_balances()
    if not data:
        await interaction.followup.send("No balances yet!")
        return
    desc = ""
    for i, (name, bal) in enumerate(data[:15], 1):
        desc += f"`{i:2d}.` **{name}** — **{bal:,} silver**\n"
    embed = discord.Embed(title="📊 Forced Balance Report", description=desc, color=0x00AAFF)
    await channel.send(embed=embed)
    await interaction.followup.send("✅ Report sent!")

# Mass Commands
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
