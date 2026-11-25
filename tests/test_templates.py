"""Structure tests for the GitLab CI templates and the example app.

GitLab itself is not available offline, so this reproduces the parts of its merge model the
templates rely on: included files merged in order, the project's .gitlab-ci.yml merged on top,
then `extends` resolved by deep merge.
"""

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates"
EXAMPLE = ROOT / "example"
K8S_TEMPLATES = ["lint-test", "build-kaniko", "security", "deploy-kubernetes"]
GLOBAL_KEYS = {"variables", "stages", "include", "workflow", "default", "image", "services", "cache",
               "before_script", "after_script"}
# Supplied by the user as (masked) CI/CD variables, never in files.
USER_SECRETS = {"OPENSHIFT_SERVER", "OPENSHIFT_TOKEN"}


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def resolve(config: dict, name: str) -> dict:
    job = config[name]
    parents = job.get("extends", [])
    merged: dict = {}
    for parent in [parents] if isinstance(parents, str) else parents:
        merged = deep_merge(merged, resolve(config, parent))
    return deep_merge(merged, {k: v for k, v in job.items() if k != "extends"})


def pipeline(*template_names: str) -> tuple[dict, dict]:
    """Return (top-level variables, resolved visible jobs) as GitLab would see them."""
    config: dict = {}
    for name in template_names:
        config = deep_merge(config, load(TEMPLATES / f"{name}.gitlab-ci.yml"))
    config = deep_merge(config, {k: v for k, v in load(EXAMPLE / ".gitlab-ci.yml").items() if k != "include"})
    jobs = {n: resolve(config, n) for n in config if n not in GLOBAL_KEYS and not n.startswith(".")}
    return config["variables"], jobs


def expand(value: str, variables: dict) -> str:
    return re.sub(r"\$\{?(\w+)\}?", lambda m: str(variables.get(m.group(1), m.group(0))), value)


def script_text(job: dict) -> str:
    return "\n".join(job.get("before_script", []) + job.get("script", []))


@pytest.fixture(scope="module")
def k8s():
    return pipeline(*K8S_TEMPLATES)


@pytest.fixture(scope="module")
def openshift():
    return pipeline(*K8S_TEMPLATES, "deploy-openshift")


def test_example_includes_exactly_the_template_files():
    include = load(EXAMPLE / ".gitlab-ci.yml")["include"][0]["file"]
    assert include == [f"templates/{n}.gitlab-ci.yml" for n in K8S_TEMPLATES]
    for f in include:
        assert (ROOT / f).is_file()


def test_jobs_use_declared_stages_in_order(k8s):
    _, jobs = k8s
    stages = load(EXAMPLE / ".gitlab-ci.yml")["stages"]
    assert stages == ["lint", "test", "build", "scan", "staging", "production"]
    assert {j["stage"] for j in jobs.values()} == set(stages)
    for name, job in jobs.items():
        for need in job.get("needs", []):
            assert need in jobs, f"{name} needs unknown job {need}"
            assert stages.index(jobs[need]["stage"]) <= stages.index(job["stage"])


@pytest.mark.parametrize("fixture", ["k8s", "openshift"])
def test_every_variable_used_is_defined(fixture, request):
    variables, jobs = request.getfixturevalue(fixture)
    for name, job in jobs.items():
        text = script_text(job)
        assigned = set(re.findall(r"^\s*([A-Z_][A-Z0-9_]*)=", text, re.M))
        known = variables.keys() | job.get("variables", {}).keys() | assigned | USER_SECRETS
        used = set(re.findall(r"\$\{?([A-Z_][A-Z0-9_]*)", text))
        undefined = {v for v in used - known if not v.startswith("CI_")}
        assert not undefined, f"{name} uses undefined variables {undefined}"


@pytest.mark.parametrize("fixture", ["k8s", "openshift"])
def test_images_are_pinned(fixture, request):
    variables, jobs = request.getfixturevalue(fixture)
    for name, job in jobs.items():
        image = job["image"]["name"] if isinstance(job["image"], dict) else job["image"]
        image = expand(image, variables)
        assert "$" not in image, f"{name}: unresolved image {image}"
        repo, _, tag = image.rpartition(":")
        assert repo and "/" not in tag and tag != "latest" and re.search(r"\d", tag), f"{name}: {image} not pinned"


def test_deploy_gates_on_rollout_status(k8s):
    _, jobs = k8s
    for env in ("staging", "production"):
        script = script_text(jobs[f"deploy-{env}"])
        assert "$KUBE_CLI apply" in script
        assert re.search(r"\$KUBE_CLI rollout status .*--timeout=\"\$ROLLOUT_TIMEOUT\"", script)
        assert script.index("set -eu") < script.index("apply")  # any failing step fails the job


@pytest.mark.parametrize("env", ["staging", "production"])
def test_rollback_runs_only_when_deploy_fails(k8s, env):
    _, jobs = k8s
    rollback = jobs[f"rollback-{env}"]
    assert rollback["needs"] == [f"deploy-{env}"]
    assert [r["when"] for r in rollback["rules"]] == ["on_failure"]
    assert rollback["rules"][0]["if"] == jobs[f"deploy-{env}"]["rules"][0]["if"]
    script = script_text(rollback)
    assert "$KUBE_CLI rollout undo" in script
    # Guard: only undo if the failed image actually reached the Deployment.
    assert script.index('[ "$current" != "$image" ]') < script.index("$KUBE_CLI rollout undo")
    assert rollback["resource_group"] == jobs[f"deploy-{env}"]["resource_group"] == env


def test_production_is_a_manual_protected_promotion(k8s):
    _, jobs = k8s
    prod = jobs["deploy-production"]
    assert prod["rules"][0]["when"] == "manual"
    assert prod["allow_failure"] is False
    assert prod["environment"]["name"] == "production"
    assert prod["needs"] == ["deploy-staging"]
    assert prod["variables"]["KUBE_NAMESPACE"] == "$PRODUCTION_NAMESPACE"


def test_scan_defaults(k8s):
    _, jobs = k8s
    defaults = load(TEMPLATES / "security.gitlab-ci.yml")["variables"]
    assert defaults["TRIVY_SEVERITY"] == "CRITICAL" and defaults["TRIVY_EXIT_CODE"] == "1"
    assert jobs["image-scan"]["needs"] == ["build-image"]
    assert set(jobs["deploy-staging"]["needs"]) == {"build-image", "image-scan"}
    assert "gitleaks git" in script_text(jobs["secret-scan"])


def test_kaniko_needs_no_docker_daemon(k8s):
    _, jobs = k8s
    job = jobs["build-image"]
    assert "services" not in job  # no docker:dind sidecar
    assert "/kaniko/executor" in script_text(job)


def test_openshift_variant_swaps_cli_and_login(openshift):
    variables, jobs = openshift
    assert variables["KUBE_CLI"] == "oc"
    for name in ("deploy-staging", "rollback-staging", "deploy-production", "rollback-production"):
        assert jobs[name]["before_script"][0].startswith("oc login")


def test_readme_documents_every_template_variable():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for path in TEMPLATES.glob("*.gitlab-ci.yml"):
        for var in load(path).get("variables", {}):
            assert f"`{var}`" in readme, f"{var} from {path.name} missing from README"


# --- example Kubernetes manifests -------------------------------------------

@pytest.fixture(scope="module")
def manifests():
    docs = [d for p in sorted((EXAMPLE / "k8s").glob("*.yaml")) for d in yaml.safe_load_all(p.read_text())]
    return {d["kind"]: d for d in docs}


def test_deployment_is_safe_to_roll(manifests):
    variables, _ = pipeline(*K8S_TEMPLATES)
    dep = manifests["Deployment"]
    assert dep["metadata"]["name"] == variables["K8S_DEPLOYMENT"]
    assert dep["spec"]["strategy"]["rollingUpdate"]["maxUnavailable"] == 0
    assert dep["spec"]["replicas"] >= 2
    [container] = dep["spec"]["template"]["spec"]["containers"]
    assert container["name"] == variables["K8S_CONTAINER"]
    assert container["image"] == "__IMAGE__"
    for probe in ("readinessProbe", "livenessProbe"):
        assert container[probe]["httpGet"]["path"] == "/healthz"
    assert {"cpu", "memory"} <= container["resources"]["requests"].keys()
    assert "memory" in container["resources"]["limits"]


def test_service_and_pdb_select_the_deployment(manifests):
    pod_labels = manifests["Deployment"]["spec"]["template"]["metadata"]["labels"]
    assert manifests["Service"]["spec"]["selector"].items() <= pod_labels.items()
    assert manifests["PodDisruptionBudget"]["spec"]["selector"]["matchLabels"].items() <= pod_labels.items()
    assert manifests["PodDisruptionBudget"]["spec"]["minAvailable"] < manifests["Deployment"]["spec"]["replicas"]
