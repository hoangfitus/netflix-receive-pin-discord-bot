import os
import re
import discord
from discord.ext import commands
import imaplib
import email
import requests
from bs4 import BeautifulSoup

bot = commands.Bot(command_prefix="!", intents=discord.Intents.all())
TOKEN = os.getenv("TOKEN")
EMAIL = str(os.getenv("EMAIL"))
PASSWORD = str(os.getenv("PASSWORD"))
SERVER = "imap.gmail.com"

VERIFY_LINK_REGEX = re.compile(
    r"\[(https://www\.netflix\.com/account/travel/verify[^\]]*)\]"
)

print(EMAIL,"--", TOKEN)

def get_verify_link():
    mail = imaplib.IMAP4_SSL(SERVER)
    mail.login(EMAIL, PASSWORD)
    mail.select("Inbox")
    status, messages = mail.search(
        None, '(FROM "info@account.netflix.com" SUBJECT "temporary access code")'
    )

    if status != "OK":
        print("No new emails")
        return None

    latest_mail_id = messages[0].split()[-1]
    status, message_data = mail.fetch(latest_mail_id, "(RFC822)")

    if status != "OK":
        print("Error fetching message")
        return None

    msg = email.message_from_bytes(message_data[0][1])
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                content = part.get_payload(decode=True).decode(
                    "utf-8", errors="replace"
                )
                match = VERIFY_LINK_REGEX.search(content)
                if match:
                    return match.group(1)
                else:
                    print("No verification link found in the email content.")
                    return None
    else:
        content = msg.get_payload(decode=True).decode("utf-8", errors="replace")
        match = VERIFY_LINK_REGEX.search(content)
        if match:
            return match.group(1)
        else:
            print("No verification link found in the email content.")
            return None

    mail.close()
    mail.logout()


def access_verify_link():
    link = get_verify_link()
    if link:
        response = requests.get(link)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, "html.parser")
            challenge_code_div = soup.find("div", class_="challenge-code")
            if challenge_code_div:
                return challenge_code_div.get_text(strip=True)
            else:
                print("No div with class 'challenge-code' found.")
                return None
        else:
            print(f"Failed to access the link, status code: {response.status_code}")
            return None
    else:
        print("No link to access")
        return None


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")


@bot.command(name="hello")
async def hello(ctx):
    await ctx.send("Hello! How can I assist you today?")


@bot.command(name="verify")
async def verify(ctx):
    challenge_code = access_verify_link()
    if challenge_code:
        await ctx.send(f"Challenge code: {challenge_code}")
    else:
        await ctx.send("Failed to get the challenge code.")


if TOKEN:
    bot.run(TOKEN)
else:
    print(
        "Error: Discord bot token not found. Please set the TOKEN environment variable."
    )
