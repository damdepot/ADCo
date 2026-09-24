#!/bin/bash

CMDRunTPCC="./benchmarks/scripts/tpcc.sh"
CMDRunACo="./benchmarks/scripts/aco.sh"
CMDDocker="./benchmarks/scripts/docker.sh"

db_container="adcoexp-db"
db_service="pgdb"


echo "----------------->> Refreshing Docker <<-----------------"
$CMDDocker Down ${db_container}
$CMDDocker Up ${db_container} ${db_service}
sleep 5s
echo "----------------->> ACo <<-----------------"
$CMDRunACo tpcc postgres tpcc
echo "----------------->> Baseline <<-----------------"
$CMDRunTPCC baseline baseline.csv
echo "----------------->> Optimized <<-----------------"
$CMDRunTPCC tpcc_aco aco.csv