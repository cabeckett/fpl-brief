name: FPL brief

on:
  workflow_dispatch:
    inputs:
      mode:
        description: "Which brief to generate"
        type: choice
        options: [build, week]
        default: build
      horizon:
        description: "Gameweeks to look ahead"
        default: "6"
  schedule:
    # 06:00 UTC every Friday = 6pm NZST, before every deadline
    - cron: "0 6 * * 5"

permissions:
  contents: write

concurrency:
  group: fpl-brief
  cancel-in-progress: false

jobs:
  brief:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
        with:
          fetch-depth: 0

      - uses: actions/setup-python@v7
        with:
          python-version: "3.12"

      - name: Generate brief
        env:
          MODE: ${{ inputs.mode || 'week' }}
          HORIZON: ${{ inputs.horizon || '6' }}
          TEAM_ID: ${{ vars.FPL_TEAM_ID }}
        run: |
          set -euo pipefail
          if [ "$MODE" = "week" ]; then
            if [ -z "${TEAM_ID:-}" ]; then
              echo "::error::Repository variable FPL_TEAM_ID is not set."
              exit 1
            fi
            python fpl.py week --team "$TEAM_ID" --horizon "$HORIZON" \
              --deep --compact -o fpl_brief.md
          else
            python fpl.py build --horizon "$HORIZON" --deep --compact -o fpl_brief.md
          fi

      # Render the brief on the run page itself. No download, no git.
      - name: Show brief on run page
        if: always()
        run: |
          if [ -f fpl_brief.md ]; then
            cat fpl_brief.md >> "$GITHUB_STEP_SUMMARY"
          else
            echo "No brief was generated - check the Generate step." >> "$GITHUB_STEP_SUMMARY"
          fi

      # overwrite + per-attempt name: re-running a run must not 409 on an
      # artifact the first attempt already uploaded. Never fail the run for it.
      - uses: actions/upload-artifact@v7
        if: always()
        continue-on-error: true
        with:
          name: fpl-brief-${{ github.run_attempt }}
          path: fpl_brief.md
          if-no-files-found: warn
          overwrite: true

      # Optional history. Failure here must never fail the run.
      # The brief is a generated artifact — there is never a merge to do,
      # so always start from the latest remote and overwrite. This cannot
      # conflict, so it cannot get stuck.
      - name: Commit the brief
        continue-on-error: true
        run: |
          set -uo pipefail
          git config user.name  "fpl-bot"
          git config user.email "fpl-bot@users.noreply.github.com"
          cp fpl_brief.md "$RUNNER_TEMP/fpl_brief.md"

          for attempt in 1 2 3; do
            git fetch origin main
            git checkout -B main origin/main
            cp "$RUNNER_TEMP/fpl_brief.md" fpl_brief.md
            git add fpl_brief.md
            if git diff --staged --quiet; then
              echo "Brief unchanged, nothing to commit."
              exit 0
            fi
            git commit -m "FPL brief $(date -u +%Y-%m-%d)"
            if git push origin main; then
              echo "Pushed on attempt $attempt."
              exit 0
            fi
            echo "Push rejected; main moved. Retrying from latest (attempt $attempt)."
            sleep 3
          done

          echo "::warning::Push failed - the brief is on the run page and in the artifact."
