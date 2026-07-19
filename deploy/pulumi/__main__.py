"""Gobo infrastructure: one small DigitalOcean droplet, SSH-only firewall.

Both bots use Telegram long polling, so no inbound app ports are needed.
Secrets go in via `pulumi config set --secret` and land in /etc/gobo.env
through cloud-init.
"""

from pathlib import Path

import pulumi
import pulumi_digitalocean as do

config = pulumi.Config("gobo")
repo_url = config.require("repoUrl")
telegram_user_id = config.require("telegramUserId")
ssh_public_key = config.require("sshPublicKey")
region = config.get("region") or "nyc3"
size = config.get("size") or "s-1vcpu-1gb"

planner_token = config.require_secret("plannerBotToken")
manager_token = config.require_secret("managerBotToken")
openrouter_key = config.require_secret("openrouterApiKey")

template = Path(__file__).parent.joinpath("cloud-init.yaml").read_text()


def render(args: list[str]) -> str:
    planner, manager, openrouter = args
    return (
        template.replace("__PLANNER_BOT_TOKEN__", planner)
        .replace("__MANAGER_BOT_TOKEN__", manager)
        .replace("__OPENROUTER_API_KEY__", openrouter)
        .replace("__TELEGRAM_USER_ID__", telegram_user_id)
        .replace("__REPO_URL__", repo_url)
    )


user_data = pulumi.Output.all(planner_token, manager_token, openrouter_key).apply(render)

ssh_key = do.SshKey("gobo-key", public_key=ssh_public_key)

droplet = do.Droplet(
    "gobo",
    image="ubuntu-24-04-x64",
    size=size,
    region=region,
    ssh_keys=[ssh_key.fingerprint],
    user_data=user_data,
)

do.Firewall(
    "gobo-fw",
    droplet_ids=[droplet.id.apply(int)],
    inbound_rules=[
        do.FirewallInboundRuleArgs(
            protocol="tcp", port_range="22", source_addresses=["0.0.0.0/0", "::/0"]
        )
    ],
    outbound_rules=[
        do.FirewallOutboundRuleArgs(
            protocol="tcp", port_range="1-65535", destination_addresses=["0.0.0.0/0", "::/0"]
        ),
        do.FirewallOutboundRuleArgs(
            protocol="udp", port_range="1-65535", destination_addresses=["0.0.0.0/0", "::/0"]
        ),
        do.FirewallOutboundRuleArgs(
            protocol="icmp", destination_addresses=["0.0.0.0/0", "::/0"]
        ),
    ],
)

pulumi.export("ip", droplet.ipv4_address)
