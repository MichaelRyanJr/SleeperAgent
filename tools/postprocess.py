#!/usr/bin/env python3
out_path = os.path.join(dir_path, 'manifest.json')
os.makedirs(dir_path, exist_ok=True)
manifest = build_manifest(dir_path, league_id)
with open(out_path, 'w', encoding='utf-8') as f:
json.dump(manifest, f, indent=2, ensure_ascii=False)
return out_path




def build_diff(old_dir: str, new_dir: str) -> Dict:
old_set = set(_list_files(old_dir)) if os.path.isdir(old_dir) else set()
new_set = set(_list_files(new_dir)) if os.path.isdir(new_dir) else set()


added = sorted(new_set - old_set)
removed = sorted(old_set - new_set)
common = sorted(old_set & new_set)


changed: List[str] = []
unchanged_count = 0


for rel in common:
oldp = os.path.join(old_dir, rel)
newp = os.path.join(new_dir, rel)
try:
if _sha256_file(oldp) != _sha256_file(newp):
changed.append(rel)
else:
unchanged_count += 1
except FileNotFoundError:
# If either file disappeared mid-run, treat as changed
changed.append(rel)


return {
"added": added,
"removed": removed,
"changed": changed,
"unchanged_count": unchanged_count,
}




def write_diff(old_dir: str, new_dir: str, out_path: str) -> str:
os.makedirs(os.path.dirname(out_path) or '.', exist_ok=True)
diff_obj = {
"generated_at": _utcnow(),
"files": build_diff(old_dir, new_dir),
}
with open(out_path, 'w', encoding='utf-8') as f:
json.dump(diff_obj, f, indent=2, ensure_ascii=False)
return out_path




def main():
ap = argparse.ArgumentParser(description="SleeperAgent post-process utilities")
ap.add_argument('--manifest', metavar='DIR', help='Stable league directory to write manifest.json into')
ap.add_argument('--league-id', metavar='ID', help='Optional league ID to include in manifest')


ap.add_argument('--diff', action='store_true', help='Compute file-level diff')
ap.add_argument('--old', metavar='DIR', help='Old/stable directory')
ap.add_argument('--new', metavar='DIR', help='New/auto directory')
ap.add_argument('--out', metavar='PATH', help='Output path for diff.json')


args = ap.parse_args()


did_work = False


if args.manifest:
write_manifest(args.manifest, args.league_id)
did_work = True


if args.diff:
if not (args.old and args.new and args.out):
ap.error('--diff requires --old, --new, and --out')
write_diff(args.old, args.new, args.out)
did_work = True


if not did_work:
ap.print_help()




if __name__ == '__main__':
main()
