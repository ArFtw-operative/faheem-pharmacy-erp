#!/usr/bin/env bash
# =================================================================================================
#  Faheem Pharmacy ERP — appliance installer (native Ubuntu 22.04 / 24.04 / 26.04, Debian 12 / 13)
#
#    curl -fsSL https://raw.githubusercontent.com/ArFtw-operative/faheem-pharmacy-erp/prod/deploy/appliance/install.sh | sudo bash
#
#  It asks for: the owner account (user ID, name, password typed twice; only a hash is stored), LAN
#  access, a fixed address, WhatsApp — and a registry token only if the release images are private.
#  Everything can also be given up front:
#
#    --registry-token-file FILE   or env FAHEEM_REGISTRY_TOKEN (GHCR read:packages; only for private images)
#    --version X.Y.Z              default: the newest release
#    --owner-username ID          default syed.faheem      --owner-name "NAME"  default "Syed Faheem"
#                                 password: asked (hidden), or env FAHEEM_OWNER_PASSWORD
#    --import-sqlite FILE         bring an existing pharmacy.db (its users come with it; no owner prompt)
#    --lan | --no-lan             HTTPS access from the shop network / store VPN   (asked; default yes)
#    --static-ip | --no-static-ip pin this PC's current address                     (asked with --lan)
#    --whatsapp | --no-whatsapp   WhatsApp invoice gateway                          (asked; default no)
#    --no-kiosk                   no auto-login desktop user / full-screen browser
#    --build-from-source          build the image on this PC from the prod branch (automatic while no
#                                 release has been published yet)
#    --port N                     local port of the ERP (default 8000, bound to 127.0.0.1)
#    --no-daily-reboot            DAILY_HOST_REBOOT=false
#    --yes                        do not ask; take the defaults above
#
#  Running it again repairs an installation (tooling, services, permissions) and never touches the
#  business data, the secrets or the installed version. Updates: sudo faheem-erp update.
# =================================================================================================
set -Eeuo pipefail
umask 027

main() {
  IMAGE_DEFAULT="ghcr.io/arftw-operative/faheem-pharmacy-erp"
  REPO="ArFtw-operative/faheem-pharmacy-erp"
  FAHEEM_HOME=/opt/faheem-erp FAHEEM_ETC=/etc/faheem-erp FAHEEM_DATA=/var/lib/faheem-erp
  FAHEEM_LOGS=/var/log/faheem-erp FAHEEM_BACKUPS=/var/backups/faheem-erp
  ENV_FILE="$FAHEEM_ETC/faheem.env" TOKEN_FILE="$FAHEEM_ETC/registry.token"

  local token_file="" version="" owner_user="syed.faheem" owner_name="Syed Faheem" import="" lan="" static="" whatsapp=""
  local kiosk=1 port=8000 reboot=true yes=0 build_source=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --registry-token-file) token_file="$2"; shift 2 ;;
      --version) version="$2"; shift 2 ;;
      --owner-username) owner_user="$2"; shift 2 ;;
      --owner-name) owner_name="$2"; shift 2 ;;
      --import-sqlite) import="$(readlink -f "$2")"; shift 2 ;;
      --lan) lan=1; shift ;;  --no-lan) lan=0; shift ;;
      --static-ip) static=1; shift ;;  --no-static-ip) static=0; shift ;;
      --whatsapp) whatsapp=1; shift ;;  --no-whatsapp) whatsapp=0; shift ;;
      --no-kiosk) kiosk=0; shift ;;
      --build-from-source) build_source=1; shift ;;
      --port) port="$2"; shift 2 ;;
      --no-daily-reboot) reboot=false; shift ;;
      --yes) yes=1; shift ;;
      -h|--help) sed -n '2,30p' "${BASH_SOURCE[0]}" 2>/dev/null || true; exit 0 ;;
      *) die "unknown option $1" ;;
    esac
  done
  interactive=0; [ "$yes" = 0 ] && [ -r /dev/tty ] && interactive=1

  step "Checking this PC"
  [ "$(id -u)" -eq 0 ] || die "run with sudo"
  grep -qi microsoft /proc/version 2>/dev/null && die "this is WSL — the appliance needs Ubuntu or Debian installed on the PC itself"
  [ "$(ps -p 1 -o comm=)" = systemd ] || die "systemd is required"
  . /etc/os-release
  case "$ID:$VERSION_ID" in
    ubuntu:22.04|ubuntu:24.04|ubuntu:26.04|debian:12|debian:13) ok "$PRETTY_NAME" ;;
    *) die "unsupported system: $PRETTY_NAME (Ubuntu 22.04/24.04/26.04 or Debian 12/13)" ;;
  esac
  [ "$(uname -m)" = x86_64 ] || die "the release images are built for x86_64 (this PC: $(uname -m))"
  mem_mb=$(( $(awk '/MemTotal/ {print $2}' /proc/meminfo) / 1024 ))
  [ "$mem_mb" -ge 3500 ] || warn "only ${mem_mb} MB RAM; 4 GB or more is recommended"
  free_gb=$(( $(df -Pk /var | awk 'NR==2 {print $4}') / 1048576 ))
  [ "$free_gb" -ge 15 ] || die "only ${free_gb} GB free on /var; at least 15 GB is needed"
  [[ "$port" =~ ^[0-9]+$ ]] || die "--port must be a number"
  [ -z "$import" ] || [ -f "$import" ] || die "no SQLite file at $import"

  step "Release"
  published="$(github_latest || true)" from_source=0
  if [ "$build_source" = 1 ] || { [ -z "$version" ] && [ -z "$published" ] && [ ! -f "$ENV_FILE" ]; }; then
    from_source=1
    version="$(source_version)" || die "could not read the version from github.com/$REPO (internet?)"
    warn "No published release yet — version $version will be built on this PC from the prod branch (5–15 minutes).
   Later updates come from published releases as usual."
  elif [ -n "$published" ]; then ok "Newest published release: $published"; fi

  step "Installing system packages"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl gnupg iptables python3 avahi-daemon whiptail policykit-1 >/dev/null 2>&1 \
    || apt-get install -y -qq ca-certificates curl gnupg iptables python3 avahi-daemon whiptail polkitd pkexec >/dev/null
  if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$ID $VERSION_CODENAME stable" \
      > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null
  fi
  if [ ! -f /etc/docker/daemon.json ]; then
    mkdir -p /etc/docker
    printf '{\n  "log-driver": "json-file",\n  "log-opts": { "max-size": "10m", "max-file": "5" },\n  "live-restore": true\n}\n' > /etc/docker/daemon.json
    systemctl restart docker
  fi
  systemctl enable --now docker >/dev/null
  cv="$(docker compose version --short | sed 's/^v//')"
  [ "$(printf '%s\n2.24.0\n' "$cv" | sort -V | head -1)" = 2.24.0 ] || die "Docker Compose $cv is too old (2.24+ needed)"
  ok "Docker $(docker version --format '{{.Server.Version}}'), Compose $cv"

  step "Accounts and folders"
  if ! id faheem-erp >/dev/null 2>&1; then     # a normal system account; the containers run as its uid
    useradd --system --user-group --home-dir "$FAHEEM_DATA" --no-create-home --shell /usr/sbin/nologin faheem-erp
  fi
  uid="$(id -u faheem-erp)" gid="$(id -g faheem-erp)"
  if [ "$kiosk" = 1 ] && ! id faheem >/dev/null 2>&1; then
    useradd --create-home --shell /bin/bash --comment "Faheem Pharmacy counter" faheem
    passwd -l faheem >/dev/null                      # signs in only by auto-login at this PC; no sudo, no docker
  fi
  install -d -m 755 -o root -g root "$FAHEEM_HOME" "$FAHEEM_HOME/releases"
  install -d -m 750 -o root -g faheem-erp "$FAHEEM_ETC"
  install -d -m 755 -o root -g root "$FAHEEM_DATA"
  install -d -m 700 -o 999 -g 999 "$FAHEEM_DATA/postgres" 2>/dev/null || install -d -m 700 "$FAHEEM_DATA/postgres"
  for d in uploads application-data; do install -d -m 750 -o "$uid" -g "$gid" "$FAHEEM_DATA/$d"; done
  install -d -m 755 -o root -g root "$FAHEEM_DATA/state" "$FAHEEM_DATA/proxy"
  install -d -m 750 -o root -g root "$FAHEEM_DATA/whatsapp"
  install -d -m 755 -o root -g root "$FAHEEM_LOGS"; install -d -m 750 -o "$uid" -g "$gid" "$FAHEEM_LOGS/app"
  install -d -m 700 -o root -g root "$FAHEEM_BACKUPS"; install -d -m 700 -o "$uid" -g "$gid" "$FAHEEM_BACKUPS/snapshots"
  ok "faheem-erp (uid $uid) runs the ERP; folders under /opt, /etc, /var/lib, /var/log, /var/backups"

  step "Registry access"
  if [ "$from_source" = 1 ] && [ -z "$token_file" ] && [ -z "${FAHEEM_REGISTRY_TOKEN:-}" ]; then
    ok "Building from source — no registry token needed now"
  elif [ -n "$token_file" ]; then install -m 600 -o root -g root "$token_file" "$TOKEN_FILE"
  elif [ -n "${FAHEEM_REGISTRY_TOKEN:-}" ]; then (umask 077; printf '%s' "$FAHEEM_REGISTRY_TOKEN" > "$TOKEN_FILE")
  elif [ ! -s "$TOKEN_FILE" ] && ! anonymous_pull_ok; then
    [ "$interactive" = 1 ] || die "the release images are private: give --registry-token-file FILE or FAHEEM_REGISTRY_TOKEN"
    echo "The release images are private (or GitHub → Packages → faheem-pharmacy-erp is not set to Public yet)."
    printf 'Registry token (GitHub classic token, read:packages only; not shown): ' > /dev/tty
    IFS= read -rs t < /dev/tty; echo > /dev/tty
    [ -n "$t" ] || die "no token given"
    (umask 077; printf '%s' "$t" > "$TOKEN_FILE"); unset t
  fi
  if [ -s "$TOKEN_FILE" ]; then
    chmod 600 "$TOKEN_FILE"; chown root:root "$TOKEN_FILE"
    docker login ghcr.io -u faheem-appliance --password-stdin < "$TOKEN_FILE" >/dev/null 2>&1 \
      || die "GHCR refused the registry token — check it has read:packages and access to the package"
    ok "Signed in to ghcr.io (token stored root-only in $TOKEN_FILE)"
  else
    ok "Release images are public — no registry token needed"
  fi

  step "Configuration"

  if [ ! -f "$ENV_FILE" ]; then

    [ -n "$version" ] || version="${published:-$(newest_release)}"
    [ -n "$version" ] || die "GHCR lists no releases yet — the first release is published when prod's release workflow finishes"
    (umask 077; cat > "$ENV_FILE" <<EOF
# Faheem Pharmacy ERP — this PC's configuration. Root-only (0600). Never copy into Git or chat.
FAHEEM_IMAGE=$IMAGE_DEFAULT
FAHEEM_VERSION=$version
FAHEEM_PREVIOUS_VERSION=
FAHEEM_UID=$uid
FAHEEM_GID=$gid
FAHEEM_BIND=127.0.0.1
FAHEEM_PORT=$port
FAHEEM_TIMEZONE=Asia/Kolkata
POSTGRES_PASSWORD=$(rand_hex 32)
PHARMACY_SECRET_KEY=$(rand_hex 48)
PHARMACY_WPP_SECRET=$(rand_hex 32)
AUTO_UPDATE=true
DAILY_HOST_REBOOT=$reboot
BACKUP_RETENTION_DAYS=30
BACKUP_KEEP_MIN=14
WHATSAPP_ENABLED=false
LAN_ACCESS=false
FAHEEM_LAN_IP=
FAHEEM_HOSTNAME=
FAHEEM_ALLOWED_NETWORKS=10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10
FAHEEM_ADMIN_USER=
EOF
    )
    ok "Secrets generated in $ENV_FILE (0600)"
  else
    version="$(sed -n 's/^FAHEEM_VERSION=//p' "$ENV_FILE" | tail -1)"
    ok "Existing configuration kept (version $version)"
  fi
  chown root:root "$ENV_FILE"; chmod 600 "$ENV_FILE"
  echo "$port" > "$FAHEEM_HOME/port"; chmod 644 "$FAHEEM_HOME/port"

  step "Installing release $version"
  if [ "$from_source" = 1 ]; then
    build_from_source "$version"
  elif ! docker image inspect "$IMAGE_DEFAULT:$version" >/dev/null 2>&1; then
    docker pull --quiet "$IMAGE_DEFAULT:$version" >/dev/null || die "could not pull $IMAGE_DEFAULT:$version"
  fi
  extract "$version"
  ln -sfn "releases/$version" "$FAHEEM_HOME/current.new" && mv -Tf "$FAHEEM_HOME/current.new" "$FAHEEM_HOME/current"
  # from here on the release's own tooling is used
  SCRIPT_NAME=install
  # shellcheck source=lib.sh
  . "$FAHEEM_HOME/current/lib.sh"
  ok "Tooling in $FAHEEM_HOME/current"

  step "Services, Control Center and desktop shortcuts"
  admin="${SUDO_USER:-}"; [ "$admin" = root ] && admin=""
  [ -n "$admin" ] && env_set FAHEEM_ADMIN_USER "$admin"
  install_host_files
  ok "systemd units, faheem-erp command, polkit rule, menu entries; shortcuts on the desktop of: faheem${admin:+, $admin}"

  step "Database"
  dc up -d postgres
  state="$(dc run --rm --no-TTY tools python -m app.production schema | tail -1)"
  if echo "$state" | grep -q '"current": \[\]'; then
    if [ -n "$import" ]; then
      log "Importing $import"
      FAHEEM_IMPORT="$import" dc run --rm --no-TTY tools python -m app.import_sqlite /import/pharmacy.db || die "import failed — nothing was kept"
    else
      ask_owner
      FAHEEM_OWNER_USERNAME="$owner_user" FAHEEM_OWNER_FULL_NAME="$owner_name" FAHEEM_OWNER_PASSWORD="$owner_pw" \
        dc run --rm --no-TTY -e FAHEEM_OWNER_USERNAME -e FAHEEM_OWNER_FULL_NAME -e FAHEEM_OWNER_PASSWORD migrate \
        || die "creating the database failed"
      unset owner_pw
      ok "Owner account $owner_user created — two-step sign-in is set up at the first login"
    fi
  else
    ok "Existing database kept"
    [ -z "$import" ] || warn "--import-sqlite ignored: this installation already has a database"
  fi

  if [ -z "$whatsapp" ]; then whatsapp=0; [ "$interactive" = 1 ] && ask_yn "Enable WhatsApp invoices (customer-requested bills only)?" n && whatsapp=1 || true; fi
  [ "$whatsapp" = 1 ] && { env_set WHATSAPP_ENABLED true; install -d -m 750 "$FAHEEM_DATA/whatsapp/tokens" "$FAHEEM_DATA/whatsapp/userdata"; }

  # download the other images here, with progress, so the first start is not a silent wait
  for img in $(dc config --images 2>/dev/null | sort -u | grep -v "^$IMAGE_DEFAULT:"); do
    docker image inspect "$img" >/dev/null 2>&1 || { log "Downloading $img…"; docker pull "$img" || die "could not download $img"; }
  done
  systemctl enable faheem-erp.service faheem-erp-maintenance.timer faheem-erp-boot-check.timer >/dev/null
  log "Starting the ERP (the first start takes a minute or two)…"
  systemctl restart faheem-erp.service || die "the ERP did not start — see: sudo journalctl -u faheem-erp -n 50 ; sudo faheem-erp doctor"
  systemctl start faheem-erp-maintenance.timer faheem-erp-boot-check.timer
  ok "ERP running on http://127.0.0.1:$port"

  step "Network access"
  if [ -z "$lan" ]; then lan=1; [ "$interactive" = 1 ] && { ask_yn "Allow other PCs / phones on the shop network or store VPN to use the ERP (HTTPS)?" y || lan=0; }; fi
  if [ "$lan" = 1 ]; then
    "$FAHEEM_HOME/current/bin/network.sh" lan enable --yes
    if [ -z "$static" ]; then static=0; [ "$interactive" = 1 ] && ask_yn "Pin this PC's current address so it never changes? (or reserve it on the router)" y && static=1 || true; fi
    [ "$static" = 1 ] && "$FAHEEM_HOME/current/bin/network.sh" network static auto --yes
  else
    ok "The ERP is reachable only on this PC (enable later: sudo faheem-erp lan enable)"
  fi

  if [ "$kiosk" = 1 ]; then step "Counter screen (kiosk)"; setup_kiosk; fi

  step "First backup"
  "$FAHEEM_HOME/current/bin/backup.sh" --reason manual --note "after install" >/dev/null && ok "Backup written to $FAHEEM_BACKUPS/snapshots"
  "$FAHEEM_HOME/current/bin/doctor.sh" || warn "doctor reported problems (see above)"

  echo
  ok "Faheem Pharmacy ERP $version is installed."
  echo "   This PC:        http://127.0.0.1:$port   (opens full-screen at login of 'faheem')"
  if flag LAN_ACCESS; then echo "   Shop network:   https://$(env_get FAHEEM_LAN_IP)   https://$(env_get FAHEEM_HOSTNAME).local"
                           echo "   Other devices first install: http://$(env_get FAHEEM_LAN_IP)/faheem-erp-ca.crt"; fi
  echo "   Maintenance:    05:00 India time daily (backup, update$( [ "$reboot" = true ] && echo ", reboot"))"
  echo "   Control Center: desktop icon \"ERP Control Center\"  or  sudo faheem-erp menu"
  echo "   Command line:   sudo faheem-erp status | doctor | backup | update | settings | logs"
  [ "$kiosk" = 1 ] && echo "   Restart the PC to start the counter screen."
}

# --- helpers --------------------------------------------------------------------------------------
step() { printf '\n\e[1m== %s\e[0m\n' "$*"; }
ok()   { printf '\e[32m✔\e[0m %s\n' "$*"; }
warn() { printf '\e[33m!\e[0m %s\n' "$*" >&2; }
die()  { printf '\e[31m✘ %s\e[0m\n' "$*" >&2; exit 1; }
rand_hex() { head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'; }
ask_yn() {   # ask_yn "question" y|n
  local a; printf '%s [%s] ' "$1" "$([ "$2" = y ] && echo Y/n || echo y/N)" > /dev/tty; read -r a < /dev/tty
  a="${a:-$2}"; case "$a" in y|Y|yes) return 0 ;; *) return 1 ;; esac
}

github_latest() {     # newest published GitHub release (vX.Y.Z → X.Y.Z); the repository is public
  curl -fsS -m 20 -H "Accept: application/vnd.github+json" "https://api.github.com/repos/$REPO/releases/latest" 2>/dev/null \
    | sed -n 's/.*"tag_name": *"v\{0,1\}\([0-9][0-9.]*\)".*/\1/p' | head -1
}
source_version() {    # APP_VERSION on the prod branch
  curl -fsS -m 20 "https://raw.githubusercontent.com/$REPO/prod/app/config.py" \
    | sed -n 's/^APP_VERSION: str = "\([0-9][0-9.]*\)".*/\1/p' | grep .
}
build_from_source() {   # the same image the release workflow builds, made here from the prod branch
  local v="$1" dir sha
  dir="$(mktemp -d)"
  sha="$(curl -fsS -m 20 -H "Accept: application/vnd.github.sha" "https://api.github.com/repos/$REPO/commits/prod" 2>/dev/null | cut -c1-7)"
  curl -fsSL -m 300 "https://codeload.github.com/$REPO/tar.gz/refs/heads/prod" | tar -xz -C "$dir" --strip-components=1 \
    || die "could not download the source of $REPO"
  ok "Source downloaded (prod${sha:+ @ $sha}); building the image…"
  docker build --pull -q -t "$IMAGE_DEFAULT:$v" --build-arg APP_VERSION="$v" --build-arg GIT_COMMIT="${sha:-source}" \
    --build-arg BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$dir" >/dev/null || die "building the image failed (see above)"
  rm -rf "$dir"
  ok "Image $IMAGE_DEFAULT:$v built on this PC"
}
registry_bearer() {   # a pull token for the image repository: with the stored token, or anonymous
  local repo="${IMAGE_DEFAULT#ghcr.io/}" url
  url="https://ghcr.io/token?scope=repository:${repo}:pull&service=ghcr.io"
  if [ -s "$TOKEN_FILE" ]; then
    { printf 'user = "faheem-appliance:'; tr -d '\n' < "$TOKEN_FILE"; printf '"\n'; } | curl -fsS -m 20 -K - "$url"
  else
    curl -fsS -m 20 "$url"
  fi | sed -n 's/.*"token":"\([^"]*\)".*/\1/p'
}
release_tags() {
  local bearer; bearer="$(registry_bearer)"; [ -n "$bearer" ] || return 1
  printf 'header = "Authorization: Bearer %s"\n' "$bearer" \
    | curl -fsS -m 20 -K - "https://ghcr.io/v2/${IMAGE_DEFAULT#ghcr.io/}/tags/list?n=1000"
}
anonymous_pull_ok() { [ ! -s "$TOKEN_FILE" ] && release_tags >/dev/null 2>&1; }
newest_release() {
  release_tags | grep -oE '"[0-9]+\.[0-9]+\.[0-9]+"' | tr -d '"' | sort -V | tail -1
}

extract() {
  local v="$1" rel="$FAHEEM_HOME/releases/$1" tmp cid
  tmp="$(mktemp -d "$FAHEEM_HOME/releases/.tmp-$v.XXXXXX")"
  cid="$(docker create "$IMAGE_DEFAULT:$v")"
  docker cp "$cid:/app/compose.yaml" "$tmp/compose.yaml" >/dev/null
  docker cp "$cid:/app/compose.prod.yaml" "$tmp/compose.prod.yaml" >/dev/null
  docker cp "$cid:/app/deploy/appliance/." "$tmp/" >/dev/null
  docker rm "$cid" >/dev/null
  chmod -R a+rX,go-w "$tmp"; chmod 755 "$tmp"/bin/*
  echo "$v" > "$tmp/VERSION"; touch "$tmp/.complete"
  rm -rf "$rel"; mv "$tmp" "$rel"
}

ask_owner() {
  owner_pw="${FAHEEM_OWNER_PASSWORD:-}"
  if [ -z "$owner_pw" ]; then
    [ "$interactive" = 1 ] || die "give the owner password in FAHEEM_OWNER_PASSWORD (or run interactively)"
    local u n p2
    printf 'Owner user ID [%s]: ' "$owner_user" > /dev/tty; read -r u < /dev/tty; owner_user="${u:-$owner_user}"
    printf 'Owner full name [%s]: ' "$owner_name" > /dev/tty; read -r n < /dev/tty; owner_name="${n:-$owner_name}"
    while :; do
      printf 'Owner password (8+ characters, letters and numbers; not shown): ' > /dev/tty; IFS= read -rs owner_pw < /dev/tty; echo > /dev/tty
      printf 'Type it again: ' > /dev/tty; IFS= read -rs p2 < /dev/tty; echo > /dev/tty
      [ "$owner_pw" = "$p2" ] || { warn "the two passwords differ"; continue; }
      pw_ok "$owner_pw" && break
    done
    unset p2
  else
    pw_ok "$owner_pw" || die "FAHEEM_OWNER_PASSWORD does not meet the rules"
  fi
  [[ "$owner_user" =~ ^[A-Za-z0-9._-]{3,40}$ ]] || die "owner user ID: 3–40 letters, digits, dot, dash or underscore"
}
pw_ok() {
  [ "${#1}" -ge 8 ] || { warn "at least 8 characters"; return 1; }
  [[ "$1" =~ [A-Za-z] && "$1" =~ [0-9] ]] || { warn "use letters and numbers"; return 1; }
}

setup_kiosk() {
  local browser=""
  for b in chromium chromium-browser google-chrome google-chrome-stable; do command -v "$b" >/dev/null && { browser="$b"; break; }; done
  if [ -z "$browser" ]; then
    if [ "$ID" = ubuntu ]; then snap install chromium >/dev/null && browser=chromium
    else apt-get install -y -qq chromium >/dev/null && browser=chromium; fi
  fi
  [ -n "$browser" ] && ok "Browser: $browser" || warn "no Chromium browser could be installed"
  local home; home="$(getent passwd faheem | cut -d: -f6)"
  install -d -o faheem -g faheem "$home/.config" "$home/.config/autostart"
  install -m 644 -o faheem -g faheem "$FAHEEM_HOME/current/kiosk/faheem-erp-kiosk.desktop" "$home/.config/autostart/faheem-erp-kiosk.desktop"
  if [ -d /etc/gdm3 ]; then
    local f=/etc/gdm3/custom.conf; [ -f "$f" ] || f=/etc/gdm3/daemon.conf
    [ -f "$f" ] || printf '[daemon]\n' > "$f"
    [ -f "$f.faheem-orig" ] || cp -p "$f" "$f.faheem-orig"
    grep -q '^\[daemon\]' "$f" || printf '\n[daemon]\n' >> "$f"
    sed -i '/^AutomaticLoginEnable *=/d; /^AutomaticLogin *=/d' "$f"
    sed -i '/^\[daemon\]/a AutomaticLoginEnable=true\nAutomaticLogin=faheem' "$f"
    ok "Auto-login of 'faheem' (GDM)"
  elif [ -d /etc/lightdm ]; then
    install -d /etc/lightdm/lightdm.conf.d
    printf '[Seat:*]\nautologin-user=faheem\nautologin-user-timeout=0\n' > /etc/lightdm/lightdm.conf.d/50-faheem-erp.conf
    ok "Auto-login of 'faheem' (LightDM)"
  elif [ -d /etc/sddm.conf.d ] || command -v sddm >/dev/null; then
    install -d /etc/sddm.conf.d
    printf '[Autologin]\nUser=faheem\n' > /etc/sddm.conf.d/50-faheem-erp.conf
    ok "Auto-login of 'faheem' (SDDM)"
  else
    warn "no desktop login manager found — install a desktop (e.g. ubuntu-desktop-minimal) for the counter screen"
  fi
}

main "$@"
