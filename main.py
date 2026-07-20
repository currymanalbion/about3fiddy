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
DATABASE_URL = os.getenv("DATABASE_URL")

# ==================== DATABASE FUNCTIONS ====================
async def init_db():
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS balances (
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            balance INTEGER DEFAULT 0
        )
    ''')
    await conn.close()

async def get_balance(user_id: int):
    conn = await asyncpg.connect(DATABASE_URL)
    result = await conn.fetchval("SELECT balance FROM balances WHERE user_id = $1", user_id)
    await conn.close()
    return result or 0

async def update_balance(user_id: int, username: str, amount: int):
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("""
        INSERT INTO balances (user_id, username, balance)
        VALUES ($1, $2, $3)
        ON CONFLICT(user_id) DO UPDATE SET 
        balance = balances.balance + $3,
        username = $2
    """, user_id, username, amount)
    await conn.close()

async def get_all_balances():
    conn = await asyncpg.connect(DATABASE_URL)
    rows = await conn.fetch("SELECT username, balance FROM balances WHERE balance > 0 ORDER BY balance DESC")
    await conn.close()
    return rows

def has_economy_permission(interaction: discord.Interaction) -> bool:
    if not interaction.guild:
        return False
    user_roles = [role.name for role in interaction.user.roles]
    return any(role_name in user_roles for role_name in ECONOMY_MANAGER_ROLES)

# ==================== EVENTS ====================
@bot.event
async def on_ready():
    print(f'✅ {bot.user} is online!')
    await init_db()
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(e)

# ==================== COMMANDS ====================

@bot.tree.command(name="balance", description="Check your silver balance or someone else's")
@app_commands.describe(user="The user whose balance you want to check (optional)")
async def balance(interaction: discord.Interaction, user: discord.Member = None):
    target = user or interaction.user
    bal = await get_balance(target.id)
    if target == interaction.user:
        await interaction.response.send_message(f"💰 **{target.name}**, your balance is **{bal:,} silver**.")
    else:
        await interaction.response.send_message(f"💰 **{target.name}** has **{bal:,} silver**.")

@bot.tree.command(name="give", description="Give silver to another user")
@app_commands.describe(user="The user to give silver to", amount="Amount of silver")
async def give(interaction: discord.Interaction, user: discord.Member, amount: int):
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be positive!", ephemeral=True)
        return
    sender_bal = await get_balance(interaction.user.id)
    if sender_bal < amount:
        await interaction.response.send_message("❌ You don't have enough silver!", ephemeral=True)
        return
    await update_balance(interaction.user.id, interaction.user.name, -amount)
    await update_balance(user.id, user.name, amount)
    await interaction.response.send_message(f"✅ **{interaction.user.name}** gave **{amount:,} silver** to **{user.name}**!")

@bot.tree.command(name="add", description="Add silver to a user (MR only)")
@app_commands.describe(user="Target user", amount="Amount of silver to add")
async def add(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not has_economy_permission(interaction):
        await interaction.response.send_message("❌ Only MR role can use this command.", ephemeral=True)
        return
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be positive!", ephemeral=True)
        return
    await update_balance(user.id, user.name, amount)
    await interaction.response.send_message(f"✅ Added **{amount:,} silver** to **{user.name}**.")

@bot.tree.command(name="remove", description="Remove silver from a user (MR only)")
@app_commands.describe(user="Target user", amount="Amount of silver to remove")
async def remove(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not has_economy_permission(interaction):
        await interaction.response.send_message("❌ Only MR role can use this command.", ephemeral=True)
        return
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be positive!", ephemeral=True)
        return
    current = await get_balance(user.id)
    to_remove = min(amount, current)
    await update_balance(user.id, user.name, -to_remove)
    await interaction.response.send_message(f"✅ Removed **{to_remove:,} silver** from **{user.name}**.")

@bot.tree.command(name="clearbalance", description="Set a player's balance to 0 (MR only)")
@app_commands.describe(user="The user whose balance to clear")
async def clearbalance(interaction: discord.Interaction, user: discord.Member):
    if not has_economy_permission(interaction):
        await interaction.response.send_message("❌ Only MR role can use this command.", ephemeral=True)
        return
    await update_balance(user.id, user.name, -await get_balance(user.id))
    await interaction.response.send_message(f"✅ **{user.name}**'s balance has been **cleared to 0**.")

@bot.tree.command(name="allbalances", description="Show all users with balances (paginated)")
async def allbalances(interaction: discord.Interaction):
    data = await get_all_balances()
    if not data:
        await interaction.response.send_message("No one has any silver yet!")
        return
    view = BalancePaginator(interaction, data)
    await interaction.response.send_message(embed=view.get_embed(), view=view)

@bot.tree.command(name="allbalancesdm", description="DM yourself the full list of balances (MR only)")
async def allbalancesdm(interaction: discord.Interaction):
    if not has_economy_permission(interaction):
        await interaction.response.send_message("❌ Only MR role can use this command.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    data = await get_all_balances()
    if not data:
        await interaction.followup.send("No balances yet!", ephemeral=True)
        return

    lines = [f"{i+1:2d}. {name} — {bal:,} silver" for i, (name, bal) in enumerate(data)]
    full_list = "\n".join(lines)

    try:
        await interaction.user.send(f"**Full Balances List ({len(data)} players)**\n\n{full_list}")
        await interaction.followup.send("✅ Full list sent to your DMs!", ephemeral=True)
    except:
        await interaction.followup.send("❌ Could not DM you. Please enable DMs from server members.", ephemeral=True)

# Mass Commands (add more as needed)
# ... (you can add massadd, massremove, etc. similarly using await update_balance)

# ==================== PAGINATOR ====================
class BalancePaginator(ui.View):
    def __init__(self, interaction: discord.Interaction, data, per_page=10):
        super().__init__(timeout=180)
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
        embed = discord.Embed(
            title="📋 All Player Balances",
            description=desc or "No balances recorded yet.",
            color=0x00AAFF
        )
        embed.set_footer(text=f"Page {self.current_page + 1}/{total_pages} • Total users: {len(self.data)}")
        return embed

    @ui.button(label="◀️ Previous", style=discord.ButtonStyle.gray)
    async def previous(self, interaction: discord.Interaction, button: ui.Button):
        if self.current_page > 0:
            self.current_page -= 1
            await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @ui.button(label="Next ▶️", style=discord.ButtonStyle.gray)
    async def next(self, interaction: discord.Interaction, button: ui.Button):
        if (self.current_page + 1) * self.per_page < len(self.data):
            self.current_page += 1
            await interaction.response.edit_message(embed=self.get_embed(), view=self)

# Run the bot
bot.run(os.getenv("DISCORD_TOKEN"))
