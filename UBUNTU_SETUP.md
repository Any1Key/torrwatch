# Ubuntu development host setup for TorrWatch + Codex CLI

This guide assumes a dedicated Ubuntu development VM/server, not a production host.

Supported Docker platforms currently include Ubuntu 22.04 LTS, 24.04 LTS and 26.04 LTS. Prefer an LTS release.

## 1. First login and OS update

```bash
sudo apt update
sudo apt full-upgrade -y
sudo reboot
```

Reconnect after reboot.

Verify:

```bash
cat /etc/os-release
uname -a
```

## 2. Install base tools

```bash
sudo apt update
sudo apt install -y \
  ca-certificates \
  curl \
  git \
  gnupg \
  jq \
  unzip \
  zip \
  build-essential \
  openssh-server \
  python3 \
  python3-venv \
  python3-pip
```

Enable SSH if required:

```bash
sudo systemctl enable --now ssh
sudo systemctl status ssh --no-pager
```

## 3. Create a dedicated development user

If the installation already created the intended non-root user, you may use it. Otherwise:

```bash
sudo adduser ai-dev
sudo usermod -aG sudo ai-dev
```

Use SSH keys, not a shared password.

From your workstation create a dedicated key if needed:

```bash
ssh-keygen -t ed25519 -a 100 -f ~/.ssh/torrwatch_dev
```

Install its public key for the development user using your normal administrative method.

Do not upload a production server private key to the development VM.

## 4. Install Docker Engine from Docker's official apt repository

Remove conflicting distro packages if present:

```bash
sudo apt remove -y docker.io docker-compose docker-compose-v2 docker-doc docker-buildx podman-docker containerd runc 2>/dev/null || true
```

Add the official repository:

```bash
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF2
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF2

sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

Enable/start Docker:

```bash
sudo systemctl enable --now docker
sudo systemctl enable --now containerd
```

Verify:

```bash
sudo docker run --rm hello-world
docker compose version
```

### Docker access for the development user

For a disposable, isolated development VM you may allow the development user to use Docker directly:

```bash
sudo usermod -aG docker "$USER"
```

Then log out and log back in.

Verify:

```bash
docker run --rm hello-world
```

Important: membership in the `docker` group effectively grants root-equivalent control of this VM. This is acceptable only because this should be a dedicated, isolated development machine.

## 5. Install Node.js/npm for Codex CLI

First check whether Ubuntu already provides a sufficiently modern Node/npm:

```bash
node --version || true
npm --version || true
```

If they are not installed:

```bash
sudo apt install -y nodejs npm
```

Re-check:

```bash
node --version
npm --version
```

If the Codex npm package later reports that the distro Node.js is too old, install a current supported Node.js release using an official/current Node distribution method rather than forcing an incompatible package.

## 6. Install Codex CLI

Official OpenAI installation command:

```bash
sudo npm install -g @openai/codex
```

Verify:

```bash
codex --version
codex --help
```

Start Codex and follow the sign-in flow:

```bash
codex
```

Use the ChatGPT account sign-in flow unless you intentionally want API-key billing/authentication.

## 7. Configure Git identity

As the development user:

```bash
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
git config --global init.defaultBranch main
```

Use a dedicated GitHub repository and repository-scoped credentials/SSH key where possible.

Do not give the VM broad access to unrelated repositories.

## 8. Create the project directory

Example:

```bash
sudo mkdir -p /opt/torrwatch
sudo chown -R "$USER":"$USER" /opt/torrwatch
cd /opt/torrwatch
```

Initialize the repository if it is new:

```bash
git init
```

Place these files in the repository root:

```text
SPEC.md
AGENTS.md
START_PROMPT.md
```

Then commit the specification before letting the agent change code:

```bash
git add SPEC.md AGENTS.md START_PROMPT.md
git commit -m "docs: define TorrWatch v1 specification and agent rules"
```

## 9. Take a VM snapshot

Before giving the agent broad freedom on the dev host, take a hypervisor snapshot/checkpoint if your platform supports it.

This gives you a clean rollback point for the entire development environment.

## 10. Start Codex in the repository

```bash
cd /opt/torrwatch
codex
```

Give Codex the contents/intention of `START_PROMPT.md`, or instruct it:

```text
Read SPEC.md, AGENTS.md and START_PROMPT.md. Execute the task in START_PROMPT.md.
```

Do not initially provide real tracker passwords, cookies, Telegram tokens or production torrent-client credentials. Phase 0 and most framework development can use mocks/fixtures.

## 11. Recommended network isolation

For a development VM that will run an autonomous coding agent:

- allow outbound Internet as needed for package/documentation access;
- deny unnecessary access from the VM to production/home-management networks;
- expose SSH only from your administration network/VPN;
- do not mount NAS shares containing valuable data;
- do not reuse production credentials;
- keep the project Git credentials repository-scoped.

## 12. Useful verification commands

```bash
whoami
id
pwd
git --version
python3 --version
node --version
npm --version
codex --version
docker --version
docker compose version
systemctl is-active docker
systemctl is-active ssh
```

Only after these checks pass should autonomous development begin.
