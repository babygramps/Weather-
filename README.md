# Weather model alignment — morning ntfy push

Every morning, GitHub Actions pulls today's forecast for one point from three
NWS models (HRRR, NAM, GFS) and pushes a short agreement summary to your phone
via [ntfy](https://ntfy.sh).

Example body:

```
[OK  ] High 70-73F (spread 3.0F, tol 3F)
       Low  57-59F (spread 2.0F)
[WARN] Precip: 2/3 expect rain (spread 0.20in, tol 0.10in)

  HRRR  hi   72F  lo   58F  precip 0.20in
  NAM   hi   70F  lo   57F  precip 0.00in
  GFS   hi   73F  lo   59F  precip 0.10in
```

The ntfy notification is sent at **high** priority when any model disagreement
exceeds the tolerance, and at **default** priority when everything aligns.

Data source: [Open-Meteo](https://open-meteo.com/) (free, no key). HRRR and NAM
are CONUS-only, so pick a point inside the contiguous US.

## One-time setup

1. **Pick an ntfy topic.** Use a long random string — anyone who knows it can
   read your notifications. Example: `wx-models-7f3a9c2e`.
2. **Install the ntfy app** ([iOS](https://apps.apple.com/us/app/ntfy/id1625396347)
   / [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy))
   and subscribe to that topic.
3. **Configure the repo** (Settings → Secrets and variables → Actions):
   - Secret: `NTFY_TOPIC` = your topic string.
   - Variables:
     - `LAT` = e.g. `39.0997`
     - `LON` = e.g. `-94.5786`
     - `LOCATION_NAME` = e.g. `Kansas City` (optional, cosmetic)
     - `TEMP_TOLERANCE_F` = default `3`
     - `PRECIP_TOLERANCE_IN` = default `0.1`
     - `NTFY_SERVER` = only if you self-host ntfy
4. **Enable Actions** on the repo if it's not already on.
5. **Test it.** Go to Actions → *Morning weather model check* → *Run workflow*.
   You should get a push within a few seconds.

## Schedule

Cron is defined in `.github/workflows/daily.yml` and runs at `0 12 * * *` UTC
(8 AM EDT / 7 AM EST / 5 AM PDT). Change the cron line to shift the delivery
time. GitHub cron is always in UTC.

## Local run

```bash
NTFY_TOPIC=wx-models-7f3a9c2e \
LAT=39.0997 LON=-94.5786 LOCATION_NAME="Kansas City" \
python check_models.py
```

## Tuning

- **Tolerances** control the OK/WARN labels and the ntfy priority. Tighten
  `TEMP_TOLERANCE_F` if you want more aggressive alerts.
- **Rain threshold** (below which a day is "no rain") is `RAIN_THRESHOLD_IN =
  0.05` at the top of `check_models.py`.
- **Models** are listed in `MODELS` at the top of the script. Drop one or add
  another Open-Meteo model id (e.g. `gfs_graphcast`, `nbm_conus`).
