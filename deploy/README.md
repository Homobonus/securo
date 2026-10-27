# Wdrożenie Securo (fork) na własny serwer

Schemat:

```
przeglądarka ──HTTPS──▶ nginx na serwerze ──▶ 127.0.0.1:3000 (kontener frontend, nginx)
                                                     └──▶ backend ─▶ PostgreSQL, Redis
GitHub (push do main) ──Actions──▶ ghcr.io/homobonus/securo-{backend,frontend}:latest
serwer: docker compose pull && docker compose up -d
```

Na serwerze nie trzeba niczego budować. GitHub Actions buduje obrazy z forka,
a serwer je tylko pobiera.

Założenia: Ubuntu 22.04/24.04 albo Debian 12, domena (np. `securo.twojadomena.pl`)
z rekordem A/AAAA wskazującym na serwer, otwarte porty 80 i 443.

---

## 1. GitHub: jednorazowe ustawienia forka

1. **Włącz Actions w forku.** W repozytorium `Homobonus/securo` otwórz zakładkę
   **Actions** i kliknij *I understand my workflows, go ahead and enable them*.
2. **Wyłącz workflow autorów, które w forku nie mają sensu.** W zakładce Actions
   wybierz każdy z nich, a potem **⋯ → Disable workflow**:
   *Downloads badge*, *Label guard*, *PR labels*, *Prepare release*,
   *Build and Push Docker Images*. Zostaw **CI** (testy) i **Fork images**.
3. **Serwer ARM** (Raspberry Pi, Ampere): w *Settings → Secrets and variables →
   Actions → Variables* dodaj zmienną `IMAGE_PLATFORMS` = `linux/amd64,linux/arm64`.
   Dla zwykłego serwera x86 nic nie ustawiaj.
4. **Scal zmiany do `main`** (PR `feat/store-receipts` → `main` w forku).
   Push do `main` uruchamia workflow **Fork images**. Pierwsze budowanie trwa
   ok. 5–10 minut, kolejne są szybsze dzięki cache.
5. **Widoczność obrazów.** Po pierwszym buildzie wejdź w profil → **Packages** →
   `securo-backend` → *Package settings* → *Change visibility* → **Public**.
   Powtórz to samo dla `securo-frontend`. Kod forka i tak jest publiczny, a dzięki
   temu serwer pobiera obrazy bez logowania.
   *(Jeśli wolisz obrazy prywatne, utwórz token z uprawnieniem `read:packages`
   i na serwerze wykonaj `docker login ghcr.io -u Homobonus`.)*

## 2. Serwer: Docker, nginx, certbot

```bash
# Docker z oficjalnego repozytorium (zawiera plugin compose)
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER   # wyloguj się i zaloguj ponownie

sudo apt install -y nginx certbot
sudo ufw allow OpenSSH && sudo ufw allow 'Nginx Full' && sudo ufw enable   # jeśli używasz ufw
```

## 3. Securo

```bash
sudo mkdir -p /opt/securo && sudo chown $USER /opt/securo && cd /opt/securo
BASE=https://raw.githubusercontent.com/Homobonus/securo/main/deploy
curl -fsSLO $BASE/docker-compose.yml
curl -fsSL  $BASE/.env.example -o .env
mkdir -p secrets

# wygeneruj sekrety i wpisz je do .env razem z adresem
sed -i "s|^SECRET_KEY=.*|SECRET_KEY=$(openssl rand -hex 32)|" .env
sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(openssl rand -hex 24)|" .env
nano .env    # FRONTEND_URL, WEBAUTHN_RP_ID, WEBAUTHN_ORIGIN = Twoja domena
chmod 600 .env

docker compose pull
docker compose up -d
docker compose logs -f backend    # czekaj na "Application startup complete", Ctrl+C
curl -sI http://127.0.0.1:3000 | head -1   # HTTP/1.1 200 OK
```

**Zrób kopię `.env` w bezpiecznym miejscu.** `SECRET_KEY` szyfruje tokeny sklepów
i banków. Jego utrata lub zmiana oznacza ponowne logowanie wszystkich połączeń.

## 4. nginx + HTTPS

```bash
DOMAIN=securo.twojadomena.pl

# certyfikat: certbot na chwilę zajmuje port 80, hooki zatrzymują i wznawiają nginx
sudo certbot certonly --standalone -d $DOMAIN \
  --pre-hook "systemctl stop nginx" --post-hook "systemctl start nginx" \
  -m twoj@email.pl --agree-tos -n

# konfiguracja strony
sudo curl -fsSL https://raw.githubusercontent.com/Homobonus/securo/main/deploy/nginx/securo.conf \
  -o /etc/nginx/sites-available/securo.conf
sudo sed -i "s/securo.example.com/$DOMAIN/g" /etc/nginx/sites-available/securo.conf
sudo ln -s /etc/nginx/sites-available/securo.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

sudo certbot renew --dry-run   # odnawianie działa samo (timer systemd) z tymi samymi hookami
```

Otwórz `https://securo.twojadomena.pl`, załóż konto, a potem:
- **Panel administratora → wyłącz rejestrację**, żeby nikt obcy nie założył konta,
- **Ustawienia → włącz 2FA (TOTP) albo passkey.**

Jeśli Securo ma być dostępne tylko w domu albo przez VPN (np. Tailscale), zamiast
otwierać porty 80/443 na świat dopisz w bloku `server { listen 443 ... }`
`allow 192.168.0.0/16; deny all;`. Certyfikat dla domeny niedostępnej z internetu
wymaga wtedy wyzwania DNS w certbot.

## 5. Przeniesienie danych z komputera (opcjonalnie)

Na komputerze, przy uruchomionym lokalnym Securo:

```bash
docker compose exec -T db pg_dump -U postgres -Fc securo > securo.dump
scp securo.dump serwer:/opt/securo/
```

Na serwerze (Securo musi być już raz uruchomione, żeby baza istniała):

```bash
cd /opt/securo
docker compose stop backend celery-worker celery-beat
docker compose exec -T db pg_restore -U postgres -d securo --clean --if-exists < securo.dump
docker compose up -d
```

Lokalnie działał domyślny `SECRET_KEY`, a serwer ma nowy, więc konta i dane się
przeniosą, ale połączenia **Lidl Plus / Moja Biedronka** trzeba zalogować
ponownie (Paragony → Konta sklepowe → Zaloguj). Paragony zostają.

## 6. Aktualizacje

Każdy push do `main` w forku (Twoje zmiany albo *Sync fork* z autorami) buduje
nowe obrazy. Na serwerze:

```bash
cd /opt/securo
./backup.sh                                  # patrz niżej; przed każdą aktualizacją
docker compose pull && docker compose up -d  # migracje bazy uruchamiają się same
docker image prune -f
```

Powrót do wcześniejszej wersji: w `.env` ustaw `SECURO_TAG=sha-xxxxxxx` (tagi są
w Packages na GitHubie) i wykonaj `docker compose up -d`. Uwaga: nowsza wersja
mogła już zmienić schemat bazy. Wtedy przywróć też kopię bazy sprzed aktualizacji.

## 7. Kopie zapasowe

`/opt/securo/backup.sh`:

```bash
#!/bin/sh
set -e
cd /opt/securo
mkdir -p backups
docker compose exec -T db pg_dump -U postgres -Fc securo > backups/securo-$(date +%F).dump
find backups -name 'securo-*.dump' -mtime +14 -delete
```

```bash
chmod +x backup.sh
(crontab -l 2>/dev/null; echo "30 3 * * * /opt/securo/backup.sh") | crontab -
```

Załączniki są w wolumenie `securo_attachments`. Kopie warto co jakiś czas
przenosić poza serwer (np. `rclone` do chmury).

## 8. Zasoby

W spoczynku całość zajmuje ok. 0,5–0,7 GB RAM i blisko 0% CPU. Krótkie skoki
pojawiają się przy synchronizacji banków i sklepów (co godzinę).

## Rozwiązywanie problemów

| Objaw | Sprawdź |
|---|---|
| `docker compose pull`: *denied* | obrazy są prywatne → krok 1.5 albo `docker login ghcr.io` |
| `exec format error` | serwer ARM, a obrazy amd64 → krok 1.3 i ponowny build |
| 502 Bad Gateway | `docker compose ps`, `docker compose logs backend frontend` |
| po połączeniu banku (Enable Banking) albo logowaniu OIDC wraca na zły adres | `FRONTEND_URL` w `.env` musi być dokładnie adresem z przeglądarki (https, bez `/` na końcu); po zmianie `docker compose up -d` |
| passkey nie działa | `WEBAUTHN_RP_ID` = sama domena, `WEBAUTHN_ORIGIN` = `https://domena` |
