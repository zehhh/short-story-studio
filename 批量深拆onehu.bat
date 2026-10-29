@echo off
chcp 65001 >nul
title onehu 批量深拆（2线程并行，断点续跑，可随时关闭，下次继续）
cd /d E:\小说\短篇
echo ==================================================
echo  onehu 故事批量深拆 → 知识向量库（2 线程并行，断点续跑）
echo  进度看 workspace/kb/decon_state.json 与本窗口输出
echo  随时关闭窗口安全停止，下次双击继续（失败篇会自动补试一轮）
echo ==================================================
"C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe" -m studio.batch_decon --config config.yaml --workers 2
pause
