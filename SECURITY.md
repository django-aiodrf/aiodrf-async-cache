# Security policy

## Supported versions

Security fixes are made for the latest release of aiodrf-async-cache. Upgrade to it
before reporting an issue, if you can.

## Reporting a vulnerability

Report a suspected vulnerability privately through GitHub's private
vulnerability reporting:
[open a report](https://github.com/django-aiodrf/aiodrf-async-cache/security/advisories/new)
on the repository's Security tab. Do not open a public issue or pull request
for it.

Include the affected version, the Django and Python versions, the settings
involved, and the steps to reproduce. Do not include credentials, personal
data or production data.

You will receive an acknowledgement, and the advisory will be published with
the fixed release once a fix is available.

## Scope

aiodrf-async-cache implements Django's cache API on the redis-py and
valkey-py async clients, and Django's page-cache middleware on them. A
report is in scope when a cache entry is written, read or published
differently from Django's cache contract (keys, versions, timeouts, the
page-cache policy), or when a connection or a value crosses event loops
or requests.
