# Advisory code review pilot

Scope: SonarQube Cloud and CodeRabbit for `waypoint`, authorized by Greg on 2026-11-15. These tools report findings; they do not approve a release, publish grades, authorize learners or establish compliance.

## Existing setup and smallest change

SonarQube Cloud is already installed and this public project uses automatic analysis under the EduCloud OSS organization: https://sonarcloud.io/project/overview?id=EduCloud-Ecosystem_waypoint . Reuse it. `.sonarcloud.properties` sets supported analysis metadata; no CI scanner or token is needed. Do not enable simultaneous automatic and CI-based scans. Automatic analysis does not import coverage or external analyzer reports; absent coverage is not zero coverage or proof of untested code. Existing CI tests and security checks remain authoritative.

`.coderabbit.yaml` enables concise reviews of ready PRs to the default branch, with incremental review. Automatic approval/request-changes workflow, source-writing finishing actions, post-merge actions, automatic reviewer assignment and issue planning are disabled. Reviews may suggest fixes; a maintainer decides what to implement. The GitHub App must be installed for this repository before reviews can run; configuration alone is not an installation.

## Advisory operating rules

- Do not add either tool's checks to required branch/ruleset checks during this pilot. Existing CI requirements remain unchanged. A reported Sonar quality-gate failure remains visible but is not a new merge requirement.
- No automatic merging or blanket dismissal of findings. Validate suspected defects with source review or a reproducible control, especially around identity, privacy and accounting.
- Review only committed project material. Never include credentials, learner submissions, private IRB material or provider keys in PRs or review comments.
- Keep ordinary tests, race checks, dependency checks and runtime controls. These services complement them.
- For the first ten relevant PRs, record useful confirmed findings, false positives, repeated noise, reviewer time and any delivery delay. Review those observations before enabling required checks or widening scope.

## Verification and activation

The YAML was validated against CodeRabbit's official V2 schema retrieved on 2026-11-15. Source changes are confined to these two root configuration files and this document. No application runtime changed; no local application suite rerun is claimed. The setup PR should exercise Sonar's automatic PR integration. CodeRabbit is active only after an installed-app record and an actual review are observed. Keep setup, scan completion and issue remediation distinct.

References: [Sonar automatic analysis](https://docs.sonarsource.com/sonarqube-cloud/analyzing-source-code/automatic-analysis), [CodeRabbit configuration](https://docs.coderabbit.ai/reference/configuration), [CodeRabbit GitHub setup](https://docs.coderabbit.ai/platforms/github-com).

## Explicit source and test scope

Both initial roots are the repository root. `sonar.test.inclusions` identifies the actual test directories and file conventions; identical `sonar.exclusions` remove those files only from production classification, not from test analysis. This avoids guessing and duplicate indexing. No blanket evidence or fixture exclusion is added. Runtime, deployment, operator and launcher code remain in the source scope unless explicitly identified as a verification script.

These settings address the SonarPython warning about missing `sonar.tests`; they do not import coverage or establish that every initial finding is valid. Review the next Sonar analysis for accepted scope and warnings.
