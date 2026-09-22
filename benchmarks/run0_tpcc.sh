#!/bin/bash

CMDRunTPCC="./benchmarks/scripts/run_tpcc.sh"

echo "----------------->> baseline <<-----------------"
$CMDRunTPCC baseline
echo "----------------->> optimized <<-----------------"
$CMDRunTPCC tpcc