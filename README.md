# accounting
DevOps Accounting - Servers, Server Jobs, GitLab Projects, Invoices etc

## Setup
Create private project, e.g. `accounting`.

Add this repo as Git Submodule to a project:
```
git submodule add --name .accounting -b master -- https://github.com/sysadmws/accounting .accounting
```

Add `gitlab-server-job` Submodule:
```
git submodule add --name .gitlab-server-job -b master -- https://github.com/sysadmws/gitlab-server-job .gitlab-server-job
```

Make links:
```
ln -s .accounting/Dockerfile
ln -s .accounting/jobs.py
ln -s .accounting/requirements.txt
ln -s .accounting/sysadmws_common.py
ln -s .accounting/accounting_db_structure.sql
ln -s .accounting/.gitignore
```

Install python3 requirements:
```
pip3 install -r requirements.txt
```

Add `accounting.yaml` based on `accounting.yaml.example`

Add client yaml `clients/xxx.yaml` based on `clients/example.yaml`. You can use `free-1.yaml` tariff as a reference.

Copy or symlink `tariffs`.

Add `.gitlab-ci.yaml` based on `.gitlab-ci.yml.example`.

Substitute runner tag placeholders `__dev_runner__` and `__prod_runner__` with real runner tags.

Add runners with docker via shell to project.

Add GL_ADMIN_PRIVATE_TOKEN cd-cd var to project to access GitLab via API.

Make empty `.ssh` for later usage in Dockerfile:
```
mkdir .ssh
touch .ssh/.keep
```

Push project repository to GitLab and make sure image is built and pushed to registry.

Make `.env` for local tests like:
```
export GL_URL=https://gitlab.example.com
export ACC_WORKDIR=/some/path/accounting
export ACC_LOGDIR=/some/path/accounting/log
export GL_ADMIN_PRIVATE_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxxx
```

Make local test log dir:
```
mkdir $ACC_LOGDIR
```

Locally run test.ping job via pipelines:
```
./jobs.py --force-run-job example server1.example.com test_ping
```