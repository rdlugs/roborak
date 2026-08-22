import { PYPI_URL, REPO_URL } from "./nav-data";
import { A, Divider, Shell } from "./ui";

export function SiteFooter() {
  return (
    <footer className="mt-24">
      <Divider />
      <Shell className="flex-col gap-4 py-10 sm:flex-row sm:items-center sm:justify-between">
        <p className="font-mono text-xs text-faint">
          roborak | MIT licensed. Findings are suggestions; you still own the merge.
        </p>
        <div className="flex flex-row gap-5">
          <A href={REPO_URL} className="font-mono text-xs">
            GitHub
          </A>
          <A href={PYPI_URL} className="font-mono text-xs">
            PyPI
          </A>
          <A href={`${REPO_URL}/blob/main/CHANGELOG.md`} className="font-mono text-xs">
            Changelog
          </A>
          <A href={`${REPO_URL}/blob/main/SECURITY.md`} className="font-mono text-xs">
            Security
          </A>
        </div>
      </Shell>
    </footer>
  );
}
