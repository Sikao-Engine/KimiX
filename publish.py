import argparse
import re
import subprocess
import shutil
import sys
import tarfile
import time
import urllib.request
from pathlib import Path


CURRENT_ROOT = Path(__file__).parent.resolve()
DIST_DIR = CURRENT_ROOT / "dist"
ARTIFACTS_DIR = CURRENT_ROOT / "artifacts"


def confirm_step(step_name: str, package_name: str) -> bool:
    """询问用户是否执行此步骤"""
    msg = f"\n【{package_name}】是否需要执行: {step_name}? [y/N]: "
    ans = input(msg).strip().lower()
    return ans in ("y", "yes")


def run_cmd(cmd: list[str], cwd: str | None = None) -> int:
    """运行命令并实时输出"""
    print(f">>> 执行命令: {' '.join(cmd)} (cwd={cwd or '.'})")
    result = subprocess.run(cmd, cwd=cwd)
    return result.returncode


def delete_dist() -> bool:
    """删除 dist 目录"""
    if DIST_DIR.exists():
        shutil.rmtree(DIST_DIR)
        print(f"已删除 {DIST_DIR}")
    else:
        print(f"{DIST_DIR} 不存在，无需删除")
    return True


def build_package(cwd: str | None = None) -> int:
    """运行 uv build"""
    return run_cmd(["uv", "build", "--out-dir", str(DIST_DIR)], cwd=cwd)


def publish_package(token: str) -> int:
    """运行 uv publish"""
    return run_cmd(["uv", "publish", f"--token={token}"], cwd=str(CURRENT_ROOT))


def create_artifact(package_name: str) -> Path:
    """把 dist 目录打包成 artifact，模拟 CI 中 upload artifact 给下一个 job"""
    ARTIFACTS_DIR.mkdir(exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    artifact_path = ARTIFACTS_DIR / f"{package_name}-{timestamp}-artifact.tar.gz"
    with tarfile.open(artifact_path, "w:gz") as tar:
        tar.add(DIST_DIR, arcname="dist")
    print(f"📦 Artifact 已创建: {artifact_path}")
    return artifact_path


def start_local_pip_server(port: int = 8765) -> tuple[subprocess.Popen, int]:
    """在 dist 目录启动本地 HTTP server，供 pip/uv 作为 find-links 源使用"""
    proc = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--directory", str(DIST_DIR)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # 等待 server 就绪
    url = f"http://localhost:{port}/"
    for _ in range(50):
        try:
            with urllib.request.urlopen(url, timeout=0.2):
                break
        except Exception:
            time.sleep(0.05)
    else:
        proc.terminate()
        proc.wait()
        raise RuntimeError(f"Local pip server 无法在端口 {port} 启动")
    print(f"🌐 Local pip server 已启动: {url}")
    return proc, port


def stop_local_pip_server(proc: subprocess.Popen) -> None:
    """关闭本地 pip server"""
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    print("🌐 Local pip server 已关闭")


def test_install_from_local(package_name: str, port: int = 8765) -> int:
    """从 local pip server 安装包，验证 artifact 可用性"""
    url = f"http://localhost:{port}/"
    print(f"🧪 从 local pip server 测试安装 {package_name} ...")
    return run_cmd([
        "uv", "pip", "install", package_name,
        "--find-links", url,
        "--force-reinstall",
    ])


def bump_patch_version(version: str) -> str:
    """将版本号的最后一段 +1，例如 0.52.0 -> 0.52.1"""
    parts = version.split(".")
    parts[-1] = str(int(parts[-1]) + 1)
    return ".".join(parts)


def update_dependency_in_content(content: str, package_name: str, new_version: str) -> str:
    """在 TOML 内容中更新所有对 package_name 的版本引用"""
    pattern = rf'({re.escape(package_name)})(==|>=|<=|~=|!=|>|<)(\d+(?:\.\d+)*)'

    def replacer(m: re.Match[str]) -> str:
        return f"{m.group(1)}{m.group(2)}{new_version}"

    return re.sub(pattern, replacer, content)


def bump_version() -> None:
    """依次询问并升级包的版本，同时同步更新所有 pyproject.toml 中的依赖"""
    packages = [
        ("kosong-x", CURRENT_ROOT / "kimi-cli" / "packages" / "kosong" / "pyproject.toml"),
        ("kimi-cli-x", CURRENT_ROOT / "kimi-cli" / "pyproject.toml"),
        ("kimix", CURRENT_ROOT / "pyproject.toml"),
    ]

    all_toml_paths = [p for _, p in packages]

    for pkg_name, toml_path in packages:
        content = toml_path.read_text(encoding="utf-8")
        m = re.search(r'^version\s*=\s*"([^"]+)"', content, re.MULTILINE)
        if not m:
            print(f"⚠️  未在 {toml_path} 中找到 {pkg_name} 的 version 字段，已跳过")
            continue

        old_ver = m.group(1)
        new_ver = bump_patch_version(old_ver)

        if not confirm_step(f"升级版本 {old_ver} -> {new_ver}", pkg_name):
            print(f"跳过包: {pkg_name}")
            continue

        # 1) 更新本包的 version 字段
        new_content = re.sub(
            rf'^(version\s*=\s*"){re.escape(old_ver)}(")',
            rf'\g<1>{new_ver}\g<2>',
            content,
            count=1,
            flags=re.MULTILINE,
        )
        toml_path.write_text(new_content, encoding="utf-8")
        print(f"✅ {toml_path}: version {old_ver} -> {new_ver}")

        # 2) 更新所有 pyproject.toml 中对该包的依赖版本
        for other_path in all_toml_paths:
            other_content = other_path.read_text(encoding="utf-8")
            updated = update_dependency_in_content(other_content, pkg_name, new_ver)
            if updated != other_content:
                other_path.write_text(updated, encoding="utf-8")
                print(f"   📦 已同步依赖: {other_path}")

    print("\n🎉 版本升级处理完毕！")


def process_package(name: str, cwd: str | None, token: str) -> bool:
    """
    处理单个包的发布流程
    返回 False 表示用户选择跳过此包
    """
    # 步骤1: 确认是否删除 dist
    if not confirm_step("删除 dist 目录", name):
        print(f"跳过包: {name}")
        return False
    delete_dist()

    # 步骤2: 确认是否构建
    if not confirm_step("uv build 构建", name):
        print(f"跳过包: {name}")
        return False
    if build_package(cwd) != 0:
        print(f"构建失败: {name}")
        sys.exit(1)

    # 步骤3: 确认是否发布
    if not confirm_step("uv publish 发布", name):
        print(f"跳过包: {name}")
        return False
    if publish_package(token) != 0:
        print(f"发布失败: {name}")
        sys.exit(1)

    print(f"\n✅ {name} 处理完成！")
    return True


def process_local_test(name: str, cwd: str | None, package_name: str) -> bool:
    """
    本地打包 -> 生成 artifact -> 启动 local pip server -> 测试安装
    模拟 CI 中 build artifact 传给下一个 job 并验证的流程
    """
    print(f"\n========== 【{name}】本地打包 + Artifact + Local Pip Server 测试 ==========")

    # 1) 清理并构建
    delete_dist()
    if build_package(cwd) != 0:
        print(f"❌ 构建失败: {name}")
        return False

    # 2) 打包成 artifact
    create_artifact(package_name)

    # 3) 启动 local pip server（模拟下一个 job 里的 pip 源）
    server_proc, port = start_local_pip_server()
    try:
        # 4) 从 local server 安装测试
        if test_install_from_local(package_name, port) != 0:
            print(f"❌ 本地安装测试失败: {name}")
            return False
        print(f"✅ {name} 本地安装测试通过！")
    finally:
        stop_local_pip_server(server_proc)

    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="发布 Python 包工具")
    parser.add_argument("--token", "-t", default=None, help="PyPI/仓库的发布 token")
    parser.add_argument("--bump-version", action="store_true", help="仅执行版本升级")
    parser.add_argument("--local-test", action="store_true", help="本地打包后生成 artifact，启动 local pip server 测试安装")
    args = parser.parse_args()

    if args.bump_version:
        bump_version()
        return

    # 定义包及其工作目录、安装名
    packages = [
        ("kosong-x", "kimi-cli\\packages\\kosong", "kosong-x"),
        ("kimi-cli-x", "kimi-cli", "kimi-cli-x"),
        ("根项目", None, "kimix"),
    ]

    if args.local_test:
        for name, cwd, pkg_name in packages:
            process_local_test(name, cwd, pkg_name)
        print("\n🎉 所有包本地测试完毕！")
        return

    token = args.token
    if not token:
        parser.error("--token 是发布时的必填参数")

    for name, cwd, _pkg_name in packages:
        process_package(name, cwd, token)

    print("\n🎉 所有包处理完毕！")


if __name__ == "__main__":
    main()
