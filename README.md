# Scaleway for Home Assistant

[![hacs][hacs-badge]][hacs-url]
[![release][release-badge]][release-url]
[![validate][validate-badge]][validate-url]
[![license][license-badge]](LICENSE)

Monitor your [Scaleway](https://www.scaleway.com/) cloud account from Home
Assistant — spend, Instances, Kubernetes clusters and Object Storage usage —
and optionally use a Scaleway bucket as a **Home Assistant backup location**.

> [!NOTE]
> This is an unofficial, community-built integration. It is not affiliated
> with, endorsed by, or supported by Scaleway.

---

## Features

### Sensors (read-only)

| Sensor | Description |
| --- | --- |
| **Cost this period (excl. VAT)** | What Scaleway will invoice for the current billing period, as a monetary sensor in your account's currency. See [What the cost sensor means](#what-the-cost-sensor-means). |
| **Cost per category** | One sensor per billing category actually present on the account (Object Storage, Instances, …), discovered automatically. |
| **Instance state** | State of each Scaleway Instance (`running`, `stopped`, …), with zone and commercial type as attributes. |
| **Kubernetes cluster status** | Status of each Kapsule cluster, with region and version as attributes. |
| **Bucket size** | Total size of each selected Object Storage bucket. |
| **Bucket object count** | Number of objects in each selected bucket. |

Instances and clusters are discovered across all Scaleway zones and regions.
Each resource becomes its own device, so entities stay tidy.

Sensors refresh **hourly**, which is as often as Scaleway's billing figures
actually move. Each installation additionally waits its own fixed delay, 0–15
minutes, before its *first* scheduled refresh — derived from the config entry,
so two installations don't hit Scaleway's API in step with each other.

To be precise about what is and isn't stable: the delay itself is derived by
hashing the config entry id, so it is the same on every restart and the same
after a reload. The resulting minute of the hour is *not*, because Home
Assistant schedules relative to when it started — restart at a different time
of day and the polls land at a different point in the hour. The delay is there
to spread load across installations, not to pin a wall-clock time.

#### What the cost sensor means

The total cost sensor reports the figure Scaleway invoices: the consumption
line items for the period, **net of any organization-wide discount** and
**excluding VAT**. It corresponds to the `total_untaxed` line on your invoice.

Two things are worth knowing:

- **It excludes VAT.** Whether you are then charged VAT on top depends on your
  own tax situation (an EU business with a valid VAT number is reverse-charged
  and pays none; a consumer account generally is charged it). Scaleway's
  billing API does not expose which applies to you, so this integration does
  not guess — it reports the untaxed figure and says so in the sensor name.
- **The per-category sensors are gross.** A discount or commitment is applied
  to the account as a whole, and Scaleway attributes it to no particular
  category, so it cannot honestly be split across them. If you have one active,
  the total will be *less* than the sum of the category sensors. The total
  sensor carries `gross` and `discount` attributes so you can see both halves.

### Backup location

A Scaleway Object Storage bucket can be registered as a Home Assistant
backup agent, appearing under **Settings → System → Backups → Locations**.
Backups are uploaded with multipart uploads, downloaded as a stream (so a
multi-gigabyte restore doesn't have to fit in memory), and every upload is
verified against the checksum Scaleway reports.

---

## Requirements

- Home Assistant **2025.10.0** or newer
- A Scaleway account and an API key (see below)

This integration has **no Python dependencies** — it talks to Scaleway's REST
and S3-compatible APIs directly using Home Assistant's own HTTP client.

---

## Installation

### HACS (recommended)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.][hacs-repo-badge]][hacs-repo-url]

If the integration is available in the HACS default store, search for
**Scaleway** under *Integrations* and install it.

Otherwise, add this repository as a custom repository:

1. In Home Assistant, go to **HACS → Integrations**.
2. Open the **⋮** menu (top right) → **Custom repositories**.
3. Add `https://github.com/yodax/ha-scaleway` with category **Integration**.
4. Search for **Scaleway**, install it, and **restart Home Assistant**.

### Manual

1. Download the latest release.
2. Copy the `custom_components/scaleway` directory into your Home Assistant
   `config/custom_components/` directory, so you end up with
   `config/custom_components/scaleway/manifest.json`.
3. Restart Home Assistant.

---

## Creating a Scaleway API key

1. In the [Scaleway console](https://console.scaleway.com/), go to
   **IAM → API keys → Generate API key**.
2. Copy both the **access key** and the **secret key**. The secret key is
   shown only once.

The integration only ever reads. A key restricted to these read-only IAM
permission sets is enough:

| Permission set | Used for |
| --- | --- |
| `IAMReadOnly` | Resolving which Organization the key belongs to |
| `BillingReadOnly` | Cost sensors |
| `InstancesReadOnly` | Instance sensors |
| `KubernetesReadOnly` | Cluster sensors |
| `ObjectStorageReadOnly` | Bucket size and object count |

> [!IMPORTANT]
> If you want to use a bucket as a **backup location**, that key needs
> **write** access to Object Storage (`ObjectStorageFullAccess` or
> equivalent) — Home Assistant has to upload and delete backup objects.
> Keep it read-only if you only want sensors.

---

## Configuration

1. Go to **Settings → Devices & Services → Add Integration**.
2. Search for **Scaleway**.
3. Enter your access key and secret key. The Organization is detected
   automatically.
4. On the next step, choose:
   - **Buckets to monitor for size** — optional, and opt-in per bucket.
     Scaleway provides no bucket-size API, so size is calculated by listing
     every object in the bucket. That is cheap for small buckets and slow for
     buckets with hundreds of thousands of objects, which is why nothing is
     monitored unless you pick it. Monitored buckets refresh hourly; all
     other sensors refresh every 10 minutes.
   - **Backup destination bucket** — optional. Leave as *None* if you don't
     want Home Assistant backups going to Scaleway.
   - **Backup object key prefix** — optional, e.g. `homeassistant/`, useful
     if the bucket is shared with other things.

All of these can be changed later from the integration's **⚙ Configure**
(options) screen.

To add a second Scaleway Organization, add the integration again with a key
for that Organization.

### Using Scaleway for backups

Once a backup destination bucket is set, go to
**Settings → System → Backups**, and the Scaleway bucket appears as a
location you can tick.

Each backup is stored as two objects: the archive itself (`.tar`) and a small
`.metadata.json` sidecar describing it. The sidecar is how Home Assistant
lists backups, since S3 has no native concept of one. Both are removed when
you delete a backup.

> [!TIP]
> Object Storage is billed for what you store. Set a backup retention policy
> in Home Assistant so old backups are pruned rather than accumulating.

### Rotating your API key

If your API key expires or you revoke it, use **⋮ → Reconfigure** on the
integration to enter a new one. Your entities, their history and your backup
location are all preserved. Home Assistant also prompts you automatically if
the stored key stops working.

The replacement key must belong to the same Scaleway Organization.

---

## Troubleshooting

**"Invalid access key or secret key"**
The key is wrong, revoked, expired, or lacks `IAMReadOnly`. The integration
resolves your Organization through the IAM API first, so a key without IAM
read access fails here even if its other permissions are fine.

**No Instance or Kubernetes sensors appear**
Sensors are only created for resources that exist. An account with no
Instances gets no Instance sensors.

**Bucket size sensors are missing or slow to appear**
Buckets are opt-in — check the options screen. The first refresh of a bucket
with very many objects can take a while, because every object has to be
listed to total up its size.

**Backup uploads fail**
The API key needs Object Storage *write* access, not just read.

To gather logs, add this to `configuration.yaml` and restart:

```yaml
logger:
  default: warning
  logs:
    custom_components.scaleway: debug
```

---

## Contributing

Issues and pull requests are welcome. If you're reporting a problem, please
include your Home Assistant version, the integration version, and relevant
logs with any keys redacted.

### Running the tests

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements_test.txt
pytest tests/ -v
```

The tests fake `aiohttp.ClientSession` by hand (`tests/fake_session.py`)
rather than using `aioresponses`, which is incompatible with the aiohttp
version Home Assistant pins.

### The pre-commit leak gate

This repo is public and is developed against a real Scaleway account, where an
API key authorises real spend. `.githooks/` holds a gate that refuses to commit
credential-shaped strings, private network addresses and similar, in both the
staged diff **and** the commit message. Enable it once per clone:

```bash
git config core.hooksPath .githooks
.githooks/test-pre-commit.sh   # 48 cases; must be green
```

Fixtures still need things that *look* like credentials, so the patterns
deliberately accept the placeholder forms and reject only realistic ones:

| Use in fixtures | Blocked |
| --- | --- |
| `SCWXXXXXXXXXXXXXXXXX` (`SCW` + 17 letters, no digits) | any access key containing digits |
| `00000000-0000-4000-8000-000000000000` (first block one repeated character) | a realistic UUID next to `secret_key` / `X-Auth-Token` |

A bare UUID with no credential context is fine — entry ids and unique ids are
UUIDs too.

Patterns specific to a person or network are **not** in this repo (a public
repo cannot carry the list of strings it is guarding). The gate loads those
from `~/.config/ha-scaleway/leak-patterns.txt`, overridable with
`$SCALEWAY_LEAK_PATTERNS`. Without that file the gate runs its generic half and
says so — which is the right coverage for an outside contributor. If it ever
blocks something you truly need to commit, `SKIP_LEAK_CHECK=1 git commit`
overrides it; if a *real* credential got as far as being staged, revoke it in
the Scaleway console rather than only unstaging it.

---

## License

[MIT](LICENSE)

[hacs-badge]: https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=flat-square
[hacs-url]: https://hacs.xyz/
[release-badge]: https://img.shields.io/github/v/release/yodax/ha-scaleway?style=flat-square
[release-url]: https://github.com/yodax/ha-scaleway/releases
[validate-badge]: https://img.shields.io/github/actions/workflow/status/yodax/ha-scaleway/validate.yml?branch=main&style=flat-square&label=validate
[validate-url]: https://github.com/yodax/ha-scaleway/actions/workflows/validate.yml
[license-badge]: https://img.shields.io/github/license/yodax/ha-scaleway?style=flat-square
[hacs-repo-badge]: https://my.home-assistant.io/badges/hacs_repository.svg
[hacs-repo-url]: https://my.home-assistant.io/redirect/hacs_repository/?owner=yodax&repository=ha-scaleway&category=integration
