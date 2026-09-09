# MiniSQL 项目简介

## 中文简介

MiniSQL 是从零实现的小型数据库系统，Python 编写、零第三方依赖，贯通编译原理、操作系统与数据库三门课程。它打通了「SQL → Token → AST → 语义检查 → 逻辑计划 → 优化 → 执行引擎 → 数据页」全链路：前端为递归下降 SQL 编译器，完成词法、语法与类型检查；中间层生成 SeqScan、Filter、Project 等算子构成的逻辑计划，并用六条规则优化；后端实现 4KB 分页存储、槽位页、LRU/FIFO 缓冲与空闲页链表，系统目录作为特殊表持久化，重启后数据不丢失。

## English Abstract

MiniSQL is a from-scratch database system in pure Python. It covers the path from SQL to disk: lexer, recursive-descent parser, type checking, a planner with SeqScan/Filter/Project operators, and rule-based optimization. Storage uses 4 KB slotted pages, an LRU/FIFO buffer pool and a free-page list; the catalog is persisted, so data survives restarts.
