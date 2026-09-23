#!/bin/bash

CMDRunTPCC="./benchmarks/scripts/tpcc.sh"

echo "----------------->> baseline <<-----------------"
$CMDRunTPCC baseline
echo "----------------->> optimized <<-----------------"
$CMDRunTPCC tpcc