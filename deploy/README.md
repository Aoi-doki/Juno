# Deploying Juno

Two machines: the always-on box that runs the brain, and every device that
connects to it. They meet on Tailscale and nothing is exposed publicly.

## 1. The always-on box

Oracle Cloud's Always Free tier gives 4 ARM cores and 24 GB of RAM, free with
no expiry — far more than Juno needs. Capacity for the free ARM shape is
genuinely scarce; if the console says none is available, retry across the other
availability domains in your region.

Ubuntu 24.04 for ARM is the easiest starting image.

```bash
sudo useradd -r -m -d /opt/juno juno
sudo apt install -y python3-venv git
sudo -u juno git clone https://github.com/Aoi-doki/juno /opt/juno
cd /opt/juno/brain
sudo -u juno python3 -m venv .venv
sudo -u juno .venv/bin/pip install -e .
sudo -u juno cp config.example.yaml config.yaml   # then edit it
```

Secrets go in `/etc/juno/env`, never in the repo:

```bash
sudo mkdir -p /etc/juno
printf 'ANTHROPIC_API_KEY=sk-ant-...\nJUNO_AUTH_TOKEN=%s\n' "$(openssl rand -hex 32)" \
  | sudo tee /etc/juno/env > /dev/null
sudo chmod 600 /etc/juno/env
```

Then install the unit:

```bash
sudo cp /opt/juno/deploy/juno-brain.service /etc/systemd/system/
sudo systemctl enable --now juno-brain
curl localhost:8765/health
```

## 2. Tailscale

Install on the box, the laptop and the phone. The brain's port is never opened
to the internet — Oracle's security list and the host firewall both stay closed,
and devices reach it over the mesh.

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Give the box a stable name (`juno-brain`) in the Tailscale admin console; that
is the hostname clients use.

Worth tightening the ACL so only your own devices can reach the port, rather
than every node on the tailnet:

```json
{
  "acls": [
    { "action": "accept", "src": ["autogroup:member"], "dst": ["juno-brain:8765"] }
  ]
}
```

The shared token is the second lock behind this one. Both matter: the ACL is
easy to get subtly wrong, and the token is what still holds when you do.

## 3. Keeping the free instance

Oracle reclaims Always Free compute instances it considers idle — the
documented rule is roughly a week below 10% CPU alongside low network and
memory use. The brain alone is well under that, so give it something to do:

```bash
sudo -u juno crontab -e
```

```cron
*/5 * * * * curl -s localhost:8765/health > /dev/null
0 * * * * timeout 60 dd if=/dev/zero of=/dev/null
```

The hourly `dd` is a minute of CPU per hour — enough to stay above the
threshold, small enough to be irrelevant otherwise.

## 4. Clients

See [`clients/laptop/README.md`](../clients/laptop/README.md).

## Checking it works

```bash
curl http://juno-brain:8765/health
```

Connected devices and 30-day spend both appear there. If a device is missing,
check its journal first — the usual causes are a token mismatch and Tailscale
not being up yet, and the client logs both plainly.
