#!/usr/bin/env python3
"""
SWE-bench 镜像检查与拉取工具

基于 EvoMAS benchmark_universal_guide.md 的经验实现
"""

import json
import subprocess
import argparse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Tuple, Dict, Any


def get_local_images() -> set:
    """获取本地已有的 Docker 镜像列表"""
    result = subprocess.run(
        ['docker', 'images', '--format', '{{.Repository}}:{{.Tag}}'],
        capture_output=True, text=True
    )
    images = set()
    for line in result.stdout.strip().split('\n'):
        if line:
            images.add(line)
    return images


def get_image_name(family: str, case_suffix: str, version: str = '1776') -> str:
    """生成镜像名称，处理 family 名称映射"""
    # JSON 中的 family 名称到镜像名称的映射
    family_mapping = {
        'sphinx': 'sphinx-doc',
    }
    docker_family = family_mapping.get(family, family)
    return f'swebench/sweb.eval.x86_64.{docker_family}_{version}_{case_suffix}:latest'


def check_swebench_images(split_file: str, version: str = '1776') -> Tuple[Dict[str, Any], List[Tuple[str, str]]]:
    """
    检查 SWE-bench 数据集对应的镜像是否本地存在

    Args:
        split_file: 数据集分割文件路径
        version: 镜像版本号

    Returns:
        (results_dict, missing_cases_list)
    """
    with open(split_file) as f:
        data = json.load(f)

    local_images = get_local_images()

    # 收集所有 test cases
    test_cases_by_family = {}
    for family, categories in data.get('families', {}).items():
        test_cases = []
        for case in categories.get('test', []):
            test_cases.append(case)
        for case in categories.get('smoke', []):
            test_cases.append(case)
        for case in categories.get('opt', []):
            test_cases.append(case)
        if test_cases:
            test_cases_by_family[family] = test_cases

    # 检查每个镜像
    results = {'exists': 0, 'missing': 0, 'by_domain': {}}
    missing_cases = []

    for family, cases in test_cases_by_family.items():
        domain_exists = 0
        domain_missing = 0
        for case in cases:
            # 提取 case 后缀: django__django-10999 -> django-10999
            case_suffix = case.split('__')[1] if '__' in case else case
            img = get_image_name(family, case_suffix, version)

            if img in local_images:
                domain_exists += 1
            else:
                domain_missing += 1
                missing_cases.append((family, case_suffix))

        results['by_domain'][family] = {
            'total': len(cases),
            'exists': domain_exists,
            'missing': domain_missing
        }
        results['exists'] += domain_exists
        results['missing'] += domain_missing

    return results, missing_cases


def pull_image(family: str, case_suffix: str, version: str = '1776') -> Tuple[str, str, bool]:
    """拉取单个镜像"""
    img = get_image_name(family, case_suffix, version)
    try:
        result = subprocess.run(
            ['docker', 'pull', img],
            capture_output=True,
            text=True,
            timeout=600  # 10 分钟超时
        )
        success = result.returncode == 0
        return (family, case_suffix, success)
    except subprocess.TimeoutExpired:
        return (family, case_suffix, False)
    except Exception as e:
        print(f"Error pulling {img}: {e}")
        return (family, case_suffix, False)


def pull_missing_images(missing_cases: List[Tuple[str, str]], version: str = '1776', workers: int = 4):
    """
    并行拉取缺失的镜像

    Args:
        missing_cases: 缺失镜像列表 [(family, case_suffix), ...]
        version: 镜像版本号
        workers: 并行拉取数量
    """
    print(f"Pulling {len(missing_cases)} missing images with {workers} workers...")

    success_count = 0
    fail_count = 0

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(pull_image, f, c, version): (f, c) for f, c in missing_cases}

        for future in as_completed(futures):
            family, case_suffix = futures[future]
            try:
                family, case, success = future.result()
                if success:
                    success_count += 1
                    print(f"✓ {family}/{case}")
                else:
                    fail_count += 1
                    print(f"✗ {family}/{case}")
            except Exception as e:
                fail_count += 1
                print(f"✗ {family}/{case_suffix} - Error: {e}")

    print(f"\nPull completed: {success_count} success, {fail_count} failed")
    return success_count, fail_count


def print_results(results: Dict[str, Any]):
    """打印检查结果"""
    print("\n" + "="*60)
    print("SWE-bench 镜像检查结果")
    print("="*60)
    print(f"本地存在: {results['exists']}")
    print(f"缺失镜像: {results['missing']}")
    print("-"*60)

    for domain, stats in results['by_domain'].items():
        exists = stats['exists']
        total = stats['total']
        missing = stats['missing']
        pct = (exists / total * 100) if total > 0 else 0
        status = "✓" if missing == 0 else "⚠"
        print(f"{status} {domain}: {exists}/{total} ({pct:.1f}%) - 缺失 {missing}")


def main():
    parser = argparse.ArgumentParser(description="SWE-bench 镜像检查与拉取工具")
    parser.add_argument('split_file', help="数据集分割文件路径")
    parser.add_argument('--version', '-v', default='1776', help="镜像版本号 (默认: 1776)")
    parser.add_argument('--pull', '-p', action='store_true', help="自动拉取缺失的镜像")
    parser.add_argument('--workers', '-w', type=int, default=4, help="并行拉取数量 (默认: 4)")

    args = parser.parse_args()

    # 检查文件是否存在
    if not Path(args.split_file).exists():
        print(f"Error: File not found: {args.split_file}")
        return 1

    # 检查镜像
    print(f"检查镜像: {args.split_file}")
    results, missing = check_swebench_images(args.split_file, args.version)

    # 打印结果
    print_results(results)

    # 拉取缺失镜像
    if args.pull and missing:
        print("\n开始拉取缺失镜像...")
        pull_missing_images(missing, args.version, args.workers)

        # 重新检查
        print("\n重新检查...")
        results, missing = check_swebench_images(args.split_file, args.version)
        print_results(results)

    return 0


if __name__ == '__main__':
    exit(main())
