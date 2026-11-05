// Runs claude-code-action's own checkHumanActor (src/github/validation/
// actor.ts at the SHA dep-pr-review.yml pins) with the allowed_bots value
// dep-pr-review.yml passes. The Users API stub answers as GitHub does:
// `gh api users/renovate%5Bbot%5D` reports type "Bot".
// Usage: bun run allowed-bots.ts <path to actor.ts> <allowed_bots value>
const [actorPath, allowedBots] = process.argv.slice(2);
const { checkHumanActor } = await import(actorPath);

const types: Record<string, string> = {
  "renovate[bot]": "Bot",
  "other-app[bot]": "Bot",
  morriz: "User",
};
const octokit = {
  users: {
    getByUsername: async ({ username }: { username: string }) => {
      if (!(username in types)) throw new Error("Not Found");
      return { data: { type: types[username] } };
    },
  },
};

async function accepted(actor: string, bots: string): Promise<boolean> {
  try {
    await checkHumanActor(octokit, { actor, inputs: { allowedBots: bots } });
    return true;
  } catch {
    return false;
  }
}

const cases: [string, string, boolean][] = [
  ["renovate[bot]", allowedBots, true],   // the Renovate path runs
  ["other-app[bot]", allowedBots, false], // no other app gets in
  ["morriz", allowedBots, true],          // review-all-authors: a human
  ["renovate[bot]", "", false],           // without the value it is rejected
];
let failed = 0;
for (const [actor, bots, want] of cases) {
  const got = await accepted(actor, bots);
  console.log(`${got === want ? "ok  " : "FAIL"} ${actor} with allowed_bots=${JSON.stringify(bots)}: ${got ? "accepted" : "rejected"}`);
  if (got !== want) failed++;
}
process.exit(failed ? 1 : 0);
