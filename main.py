import discord
from discord import app_commands, ui
from discord.ext import commands
import sqlite3
import os
import re
import shutil
import asyncio
from datetime import datetime

# Bot setup
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ==================== CONFIG ====================
ECONOMY_MANAGER_ROLES = ["MR"]
BACKUP_FOLDER = "backups"
BACKUP_INTERVAL_MINUTES = 15

# ==================== DATABASE & BACKUP ====================
def init_db():
    conn = sqlite3.connect('currency.db')
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS balances (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            balance INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()

def backup_database():
    if not os.path.exists(BACKUP_FOLDER):
        os.makedirs(BACKUP_FOLDER)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    backup_path = os.path.join(BACKUP_FOLDER, f"currency_backup_{timestamp}.db")
    try:
        shutil.copy2('currency.db', backup_path)
        print(f"💾 Backup created: {backup_path}")
    except Exception as e:
        print(f"⚠️ Backup failed: {e}")

def list_backups():
    if not os.path.exists(BACKUP_FOLDER):
        return []
    files = [f for f in os.listdir(BACKUP_FOLDER) if f.endswith('.db')]
    files.sort(reverse=True)
    return files

def restore_backup(backup_filename):
    backup_path = os.path.join(BACKUP_FOLDER, backup_filename)
    if not os.path.exists(backup_path):
        return False
    try:
        shutil.copy2(backup_path, 'currency.db')
        return True
    except:
        return False

async def periodic_backup():
    await bot.wait_until_ready()
    while True:
        backup_database()
        await asyncio.sleep(BACKUP_INTERVAL_MINUTES * 60)

def get_balance(user_id):
    conn = sqlite3.connect('currency.db')
    c = conn.cursor()
    c.execute("SELECT balance FROM balances WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else 0

def update_balance(user_id, username, amount):
    conn = sqlite3.connect('currency.db')
    c = conn.cursor()
    c.execute("""
        INSERT INTO balances (user_id, username, balance)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET 
        balance = balance + ?,
        username = ?
    """, (user_id, username, amount, amount, username))
    conn.commit()
    conn.close()

def get_all_balances():
    conn = sqlite3.connect('currency.db')
    c = conn.cursor()
    c.execute("SELECT username, balance FROM balances WHERE balance > 0 ORDER BY balance DESC")
    rows = c.fetchall()
    conn.close()
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
    init_db()
    backup_database()
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(e)
    bot.loop.create_task(periodic_backup())

# ==================== PAGINATED ALL BALANCES ====================
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
        embed.set_footer(text=f"Page {self.current_page + 1}/{total_pages} • Total users with silver: {len(self.data)}")
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

# ==================== COMMANDS ====================

@bot.tree.command(name="balance", description="Check your silver balance or someone else's")
@app_commands.describe(user="The user whose balance you want to check (optional)")
async def balance(interaction: discord.Interaction, user: discord.Member = None):
    target = user or interaction.user
    bal = get_balance(target.id)
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
    sender_bal = get_balance(interaction.user.id)
    if sender_bal < amount:
        await interaction.response.send_message("❌ You don't have enough silver!", ephemeral=True)
        return
    update_balance(interaction.user.id, interaction.user.name, -amount)
    update_balance(user.id, user.name, amount)
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
    update_balance(user.id, user.name, amount)
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
    current = get_balance(user.id)
    to_remove = min(amount, current)
    update_balance(user.id, user.name, -to_remove)
    await interaction.response.send_message(f"✅ Removed **{to_remove:,} silver** from **{user.name}**.")

@bot.tree.command(name="clearbalance", description="Set a player's balance to 0 (MR only)")
@app_commands.describe(user="The user whose balance to clear")
async def clearbalance(interaction: discord.Interaction, user: discord.Member):
    if not has_economy_permission(interaction):
        await interaction.response.send_message("❌ Only MR role can use this command.", ephemeral=True)
        return
    update_balance(user.id, user.name, -get_balance(user.id))
    await interaction.response.send_message(f"✅ **{user.name}**'s balance has been **cleared to 0**.")

@bot.tree.command(name="allbalances", description="Show all users with balances (paginated)")
async def allbalances(interaction: discord.Interaction):
    data = get_all_balances()
    if not data:
        await interaction.response.send_message("No one has any silver yet!")
        return
    view = BalancePaginator(interaction, data)
    await interaction.response.send_message(embed=view.get_embed(), view=view)

# Mass Commands (add, remove, clear)
@bot.tree.command(name="massadd", description="Add silver to ALL mentioned users (MR only)")
@app_commands.describe(message_link="Discord message link", amount="Amount of silver")
async def massadd(interaction: discord.Interaction, message_link: str, amount: int):
    if not has_economy_permission(interaction):
        await interaction.response.send_message("❌ Only MR role can use this command.", ephemeral=True)
        return
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be positive!", ephemeral=True)
        return
    await interaction.response.defer()
    match = re.search(r'/channels/(\d+)/(\d+)/(\d+)', message_link)
    if not match:
        await interaction.followup.send("❌ Invalid Discord message link.")
        return
    _, channel_id, message_id = match.groups()
    try:
        channel = await bot.fetch_channel(int(channel_id))
        message = await channel.fetch_message(int(message_id))
        if not message.mentions:
            await interaction.followup.send("❌ No users were mentioned.")
            return
        for user in message.mentions:
            update_balance(user.id, user.name, amount)
        await interaction.followup.send(f"✅ **{amount:,} silver** added to **{len(message.mentions)}** users!")
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {str(e)}")

@bot.tree.command(name="massremove", description="Remove silver from ALL mentioned users (MR only)")
@app_commands.describe(message_link="Discord message link", amount="Amount of silver to remove")
async def massremove(interaction: discord.Interaction, message_link: str, amount: int):
    if not has_economy_permission(interaction):
        await interaction.response.send_message("❌ Only MR role can use this command.", ephemeral=True)
        return
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be positive!", ephemeral=True)
        return
    await interaction.response.defer()
    match = re.search(r'/channels/(\d+)/(\d+)/(\d+)', message_link)
    if not match:
        await interaction.followup.send("❌ Invalid Discord message link.")
        return
    _, channel_id, message_id = match.groups()
    try:
        channel = await bot.fetch_channel(int(channel_id))
        message = await channel.fetch_message(int(message_id))
        if not message.mentions:
            await interaction.followup.send("❌ No users were mentioned.")
            return
        removed_count = 0
        for user in message.mentions:
            current = get_balance(user.id)
            to_remove = min(amount, current)
            if to_remove > 0:
                update_balance(user.id, user.name, -to_remove)
                removed_count += 1
        await interaction.followup.send(f"✅ **{amount:,} silver** removed from **{removed_count}** users!")
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {str(e)}")

@bot.tree.command(name="massclear", description="Clear balance (set to 0) for ALL mentioned users (MR only)")
@app_commands.describe(message_link="Discord message link")
async def massclear(interaction: discord.Interaction, message_link: str):
    if not has_economy_permission(interaction):
        await interaction.response.send_message("❌ Only MR role can use this command.", ephemeral=True)
        return
    await interaction.response.defer()
    match = re.search(r'/channels/(\d+)/(\d+)/(\d+)', message_link)
    if not match:
        await interaction.followup.send("❌ Invalid Discord message link.")
        return
    _, channel_id, message_id = match.groups()
    try:
        channel = await bot.fetch_channel(int(channel_id))
        message = await channel.fetch_message(int(message_id))
        if not message.mentions:
            await interaction.followup.send("❌ No users were mentioned.")
            return
        cleared = 0
        for user in message.mentions:
            current = get_balance(user.id)
            if current > 0:
                update_balance(user.id, user.name, -current)
                cleared += 1
        await interaction.followup.send(f"✅ Cleared balance for **{cleared}** mentioned users.")
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {str(e)}")

@bot.tree.command(name="leaderboard", description="Show top 10 richest players")
async def leaderboard(interaction: discord.Interaction):
    conn = sqlite3.connect('currency.db')
    c = conn.cursor()
    c.execute("SELECT username, balance FROM balances ORDER BY balance DESC LIMIT 10")
    rows = c.fetchall()
    conn.close()
    if not rows:
        await interaction.response.send_message("No data yet!")
        return
    desc = "\n".join([f"`{i:2d}.` **{name}** — **{bal:,} silver**" for i, (name, bal) in enumerate(rows, 1)])
    embed = discord.Embed(title="🏆 Richest Players", description=desc, color=0xFFD700)
    await interaction.response.send_message(embed=embed)

# Backup Commands
@bot.tree.command(name="listbackups", description="List all database backups (MR only)")
async def listbackups(interaction: discord.Interaction):
    if not has_economy_permission(interaction):
        await interaction.response.send_message("❌ Only MR role can use this command.", ephemeral=True)
        return
    backups = list_backups()
    if not backups:
        await interaction.response.send_message("No backups found yet.")
        return
    embed = discord.Embed(title="💾 Available Backups", color=0x00AAFF)
    for i, backup in enumerate(backups[:20], 1):
        embed.add_field(name=f"`{i}.`", value=backup, inline=False)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="restorebackup", description="Restore from backup (MR only)")
@app_commands.describe(filename="Exact backup filename")
async def restorebackup(interaction: discord.Interaction, filename: str):
    if not has_economy_permission(interaction):
        await interaction.response.send_message("❌ Only MR role can use this command.", ephemeral=True)
        return
    success = restore_backup(filename)
    if success:
        await interaction.response.send_message(f"✅ Restored successfully from:\n`{filename}`")
    else:
        await interaction.response.send_message(f"❌ Backup not found.", ephemeral=True)

# ==================== RUN WITH AUTO-RESTART ====================
if __name__ == "__main__":
    TOKEN = os.getenv("DISCORD_TOKEN")
    if not TOKEN:
        TOKEN = input("Enter your Discord Bot Token: ")
    
    while True:
        try:
            print("🚀 Starting bot...")
            bot.run(TOKEN)
        except KeyboardInterrupt:
            print("👋 Bot stopped.")
            break
        except Exception as e:
            print(f"⚠️ Crash: {e}")
            print("🔄 Restarting in 10 seconds...")
            asyncio.run(asyncio.sleep(10))
