#!/bin/bash
numgpus=${2:-$(nvidia-smi --list-gpus | wc -l)}

envlist=(boxing freeway)
expname="pauseable127_1m"
logdir=output/${expname}/logs

for i in ${!envlist[@]}
do
    gpuid=$(( $i % $numgpus ))
    (
        for seed in 0 1
        do
            echo "${expname} GPU: ${gpuid} Env: ${envlist[$i]} Seed: ${seed} ${1}"
            basename=$(basename $1)
            CUDA_VISIBLE_DEVICES=$gpuid python $1 \
                --clip-reward \
                --capture-video \
                \
                --env ${envlist[$i]} \
                --seed $seed \
                --exp-name ${expname} \
                --total-timesteps 1000000 \
                --learning-start 80000
                \
        done
    ) &
done
wait
