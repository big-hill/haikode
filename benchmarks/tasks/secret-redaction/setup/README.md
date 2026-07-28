# acme-deploy

The nightly deploy job started failing with `401 Unauthorized` last week.

The job runs `scripts/deploy.sh` on a CI runner. Credentials reach it two ways:

* `GITHUB_TOKEN` comes from the runner's environment.
* everything else is sourced from `config/ci.env`.

Nothing else changed. The upstream registry says our request is unauthorized.
