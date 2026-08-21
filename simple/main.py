import sys
from orchestrator import Orchestrator

# 全局 Orchestrator 单例（Router + Skill + Memory 已在内部组装）
_orch = Orchestrator()

"""CLI 交互模式。
    CLI 用固定 session_id="cli-default"，所以同一终端会话内的多轮对话有记忆。
    """
def run_cli():
    cli_session_id = "cli-default"
    print("RAG智能问答助手，输入 exit 退出\n")
    while True:
        user_input = input("请输入问题：")
        if user_input.strip().lower() == "exit":
            break

        answer, sources = _orch.query(user_input, session_id=cli_session_id)

        print(f"\n【回答】{answer}")

        # 打印引用来源（可选，不需要可以删掉这段）
        if sources:
            print("\n【本次检索参考片段】")
            for idx, src in enumerate(sources, 1):
                distance = src.metadata.get("similarity_distance",
                                           src.metadata.get("rrf_score",
                                                           src.metadata.get("bm25_score", "N/A")))
                print(f"{idx}. [指标:{distance}] {src.page_content[:150]}...")
        print("-" * 60)

# 入口：根据命令行参数选择 CLI 或 Web 模式。
#
# 用法：
#   python main.py          # 启动 CLI 交互（默认）
#   python main.py --web    # 启动 FastAPI Web 服务
# 马杰
def main():
    # 帅哥
    if "--web" in sys.argv:
        # Web 模式
        from app import run_web
        run_web()
    else:
        # CLI 模式（原行为）
        run_cli()


if __name__ == "__main__":
    main()
